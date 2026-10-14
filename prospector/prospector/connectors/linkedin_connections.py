"""Owner's own LinkedIn data export → "people you already know" (plan §4.1 route 2).

Input is the `Connections.csv` file from LinkedIn's *Get a copy of your data* download. It has a
free-text preamble ("Notes: …") before the header line
`First Name,Last Name,URL,Email Address,Company,Position,Connected On`. Nothing is fetched from
LinkedIn; the file belongs to the owner and describes relationships they already have.

Rules:
- owner seat only (enforced by the route);
- each row becomes at most one `Person` with lawful basis `legitimate_interest` and the LIA
  reference from settings, `notice_status=pending`, `source=linkedin_connections`;
- the person is linked to an organisation only when the company name matches an existing one
  (strict fuzzy match); unmatched rows stay unlinked and are linked automatically later;
- re-importing updates rows by profile URL instead of duplicating them;
- these contacts are for the owner's personal, one-to-one outreach — they are excluded from the
  bulk persons export.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import datetime

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..compliance import audit
from ..core.dedupe import find_existing
from ..core.normalize import normalize_email, normalize_name
from ..models import ImportBatch, Organization, Person, User
from ..util import utcnow
from .base import Connector, register

HEADER_START = "first name"
COLUMNS = {"first name": "first_name", "last name": "last_name", "url": "url", "email address": "email",
           "company": "company", "position": "position", "connected on": "connected_on"}
LINK_THRESHOLD = 93  # token_set_ratio between normalised names


@dataclass
class ConnectionRow:
    first_name: str = ""
    last_name: str = ""
    url: str = ""
    email: str = ""
    company: str = ""
    position: str = ""
    connected_on: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(p for p in (self.first_name.strip(), self.last_name.strip()) if p)


@dataclass
class ConnectionsSummary:
    rows: int = 0
    created: int = 0
    updated: int = 0
    linked: int = 0
    unlinked: int = 0
    skipped: int = 0
    errors: list[dict] = field(default_factory=list)


def parse_connections_csv(content: bytes) -> list[ConnectionRow]:
    """Skip LinkedIn's preamble, then read rows by header name (column order may change)."""
    text = content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip().lower().startswith(HEADER_START)), None)
    if start is None:
        raise ValueError("not a LinkedIn Connections.csv: header row 'First Name,…' not found")
    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    rows: list[ConnectionRow] = []
    for raw in reader:
        data = {COLUMNS[k.strip().lower()]: (v or "").strip() for k, v in raw.items() if k and k.strip().lower() in COLUMNS}
        row = ConnectionRow(**data)
        if row.full_name:
            rows.append(row)
    return rows


def _parse_connected_on(value: str) -> str | None:
    for fmt in ("%d %b %Y", "%d-%b-%y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return value.strip() or None


def match_company(session: Session, company: str) -> Organization | None:
    """Strict name match against existing organisations (no city → threshold 97 in `find_existing`)."""
    if not company or not normalize_name(company):
        return None
    return find_existing(session, name=company)


def link_known_contacts(session: Session, org: Organization) -> int:
    """Attach still-unlinked connections whose company name matches this organisation."""
    names = [normalize_name(n) for n in (org.display_name, org.legal_name, org.trade_name) if n]
    if not names:
        return 0
    linked = 0
    for person in session.scalars(select(Person).where(Person.organization_id.is_(None), Person.source == "linkedin_connections")):
        company = normalize_name((person.meta or {}).get("company"))
        if not company:
            continue
        if any(fuzz.token_set_ratio(company, name) >= LINK_THRESHOLD for name in names):
            person.organization = org
            linked += 1
    if linked:
        session.flush()
    return linked


@register
class LinkedInConnectionsConnector(Connector):
    """Local file import; no API, no terms clock. `creates_persons` is true because the owner
    (a human with an existing relationship) supplies the rows."""

    name = "linkedin_connections"
    terms_url = "https://www.linkedin.com/legal/user-agreement"
    requires_terms_review = False
    default_daily_cap = 100_000
    creates_persons = True
    person_route = "owner_export"

    def import_rows(self, session: Session, batch: ImportBatch, rows: list[ConnectionRow], *, actor: User) -> ConnectionsSummary:
        summary = ConnectionsSummary(rows=len(rows))
        now = utcnow()
        lia = self.settings.linkedin_connections_lia_reference
        for index, row in enumerate(rows, start=1):
            try:
                email = normalize_email(row.email) if row.email else None
                person = None
                if row.url:
                    person = session.scalar(select(Person).where(Person.linkedin_url == row.url, Person.source == "linkedin_connections"))
                if person is None and email:
                    person = session.scalar(select(Person).where(Person.email == email, Person.source == "linkedin_connections"))
                org = match_company(session, row.company) if row.company else None
                meta = {"company": row.company or None, "connected_on": _parse_connected_on(row.connected_on), "import_batch": batch.id}
                if person is None:
                    person = Person(
                        organization=org, full_name=row.full_name[:200], role_title=(row.position or None), email=email,
                        linkedin_url=row.url or None, source="linkedin_connections", lawful_basis="legitimate_interest",
                        lia_reference=lia, notice_status="pending", created_by=actor.id, meta=meta,
                    )
                    session.add(person)
                    summary.created += 1
                else:
                    person.full_name = row.full_name[:200]
                    person.role_title = row.position or person.role_title
                    person.email = email or person.email
                    if person.organization_id is None and org is not None:
                        person.organization = org
                    person.meta = {**(person.meta or {}), **meta}
                    person.updated_at = now
                    summary.updated += 1
                if person.organization_id is not None or org is not None:
                    summary.linked += 1
                else:
                    summary.unlinked += 1
            except Exception as exc:  # row-level report, import continues
                summary.errors.append({"row": index, "error": str(exc)[:200]})
                summary.skipped += 1
        session.flush()
        batch.row_count = summary.rows
        batch.rows_ok = summary.rows - summary.skipped
        batch.rows_error = summary.skipped
        batch.persons_created = summary.created
        batch.organizations_matched = summary.linked
        batch.errors = summary.errors[:200]
        audit.log(session, "import.linkedin_connections", actor=actor, entity_type="import_batch", entity_id=batch.id,
                  rows=summary.rows, created=summary.created, updated=summary.updated, linked=summary.linked, unlinked=summary.unlinked)
        return summary
