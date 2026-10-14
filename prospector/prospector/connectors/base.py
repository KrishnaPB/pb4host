"""Connector base class and registry.

Guards implemented here:
- R2  automated connectors can never create person records (`creates_persons` is False and any
      person-level fact they emit is rejected);
- R10 a connector refuses to run while disabled or while its terms review is older than the
      configured maximum age;
- R11 daily call caps are reserved atomically before an external request is made.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, ClassVar

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..compliance.policies import PassthroughPolicy, StoragePolicy
from ..config import Settings, get_settings
from ..core.facts import Fact
from ..models import ConnectorState
from ..util import utcnow


class ConnectorError(RuntimeError):
    pass


class ConnectorDisabled(ConnectorError):
    pass


class TermsReviewRequired(ConnectorError):
    pass


class CapExceeded(ConnectorError):
    pass


class PersonFactRejected(ConnectorError):
    pass


@dataclass
class Candidate:
    """A discovered organisation pointer. `persist` has already passed the storage policy;
    `transient` is used in-process only (e.g. the website hint) and is never written."""

    provider: str
    external_id: str
    track: str = "unknown"
    persist: dict[str, Any] = field(default_factory=dict)
    transient: dict[str, Any] = field(default_factory=dict)

    @property
    def website_hint(self) -> str | None:
        return self.transient.get("website")


class Connector:
    name: ClassVar[str] = "base"
    terms_url: ClassVar[str] = ""
    requires_terms_review: ClassVar[bool] = True
    default_daily_cap: ClassVar[int] = 1000
    cost_per_call_usd: ClassVar[float] = 0.0
    creates_persons: ClassVar[bool] = False  # R2: automated connectors never create persons
    # Why a connector may create persons at all; None for every automated source.
    # "import"  → a human uploaded rows and chose a lawful basis at import time
    # "consent" → the person themselves submitted the form / signed in
    # "owner_export" → the owner's own relationship data (LinkedIn connections export)
    person_route: ClassVar[str | None] = None
    policy: StoragePolicy = PassthroughPolicy()

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # --- state -----------------------------------------------------------------------------
    def state(self, session: Session) -> ConnectorState:
        row = session.scalar(select(ConnectorState).where(ConnectorState.name == self.name))
        if row is None:
            row = ConnectorState(
                name=self.name,
                enabled=True,
                requires_terms_review=self.requires_terms_review,
                terms_url=self.terms_url or None,
                daily_cap=self.default_daily_cap,
            )
            session.add(row)
            session.flush()
        return row

    def guard(self, session: Session) -> ConnectorState:
        """R10: refuse to run when disabled or when the terms review is stale."""
        row = self.state(session)
        if not row.enabled:
            raise ConnectorDisabled(f"connector {self.name} is disabled (kill switch)")
        if row.requires_terms_review:
            max_age = timedelta(days=self.settings.terms_review_max_age_days)
            if row.terms_reviewed_at is None:
                raise TermsReviewRequired(
                    f"connector {self.name}: terms have never been reviewed; "
                    f"review {row.terms_url or 'the source terms'} and mark it reviewed"
                )
            if utcnow() - row.terms_reviewed_at > max_age:
                raise TermsReviewRequired(
                    f"connector {self.name}: terms review is older than "
                    f"{self.settings.terms_review_max_age_days} days"
                )
        return row

    def reserve_calls(self, session: Session, n: int = 1) -> None:
        """R11: atomically count calls against today's cap before making them."""
        row = self.guard(session)
        today = utcnow().date()
        if row.calls_date != today:
            row.calls_date = today
            row.calls_today = 0
            session.flush()
        result = session.execute(
            update(ConnectorState)
            .where(
                ConnectorState.id == row.id,
                ConnectorState.calls_date == today,
                ConnectorState.calls_today + n <= ConnectorState.daily_cap,
            )
            .values(calls_today=ConnectorState.calls_today + n, last_called_at=utcnow())
        )
        if result.rowcount != 1:
            session.rollback()
            raise CapExceeded(
                f"connector {self.name}: daily cap of {row.daily_cap} calls reached"
            )
        session.commit()
        session.refresh(row)

    def record_error(self, session: Session, error: str) -> None:
        row = self.state(session)
        row.last_error = error[:2000]
        session.commit()

    # --- R2 ------------------------------------------------------------------------------
    def check_facts(self, facts: list[Fact]) -> list[Fact]:
        if not self.creates_persons:
            for fact in facts:
                if fact.entity_type == "person":
                    raise PersonFactRejected(
                        f"connector {self.name} may not create person data (R2)"
                    )
        return facts


REGISTRY: dict[str, type[Connector]] = {}


def register(cls: type[Connector]) -> type[Connector]:
    REGISTRY[cls.name] = cls
    return cls


def ensure_connector_states(session: Session) -> list[ConnectorState]:
    # Import modules so they register themselves
    from ..crm import sync  # noqa: F401
    from . import (  # noqa: F401
        companies_house,
        crux,
        csv_import,
        dns_asn,
        github_orgs,
        google_places,
        httparchive_feed,
        inbound_webhook,
        linkedin_connections,
        linkedin_oidc,
        pagespeed,
    )
    from .website_probe import probe  # noqa: F401

    rows = []
    for cls in REGISTRY.values():
        rows.append(cls().state(session))
    session.commit()
    return rows
