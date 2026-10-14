"""ORM models — the data model from §8 of the implementation plan."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base
from .util import utcnow

ORG_STATUSES = (
    "new",
    "reviewing",
    "qualified",
    "exported",
    "contacted",
    "customer",
    "disqualified",
    "suppressed",
    "merged",
    "anonymised",
)
TRACKS = ("smb", "agency", "unknown")
LAWFUL_BASES = ("consent", "contract", "legitimate_interest", "manual_research")
NOTICE_STATUSES = ("not_required", "pending", "sent", "objected")
SUPPRESSION_KINDS = ("domain", "email", "phone", "place_id")
ROLES = ("owner", "analyst", "viewer")
ERROR_STATES = (
    "none",
    "server_error",
    "db_error",
    "php_error",
    "maintenance",
    "dns_fail",
    "tls_invalid",
    "parked",
    "timeout",
    "unreachable",
)

JSONList = MutableList.as_mutable(JSON)
JSONDict = MutableDict.as_mutable(JSON)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class User(Base, TimestampMixin):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="analyst")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (CheckConstraint(f"role in {ROLES}", name="ck_user_role"),)


class Organization(Base, TimestampMixin):
    __tablename__ = "organization"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(255))
    legal_name: Mapped[str | None] = mapped_column(String(255))
    trade_name: Mapped[str | None] = mapped_column(String(255))
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    website_url: Mapped[str | None] = mapped_column(Text)
    country: Mapped[str | None] = mapped_column(String(2))
    region: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    address_text: Mapped[str | None] = mapped_column(Text)
    phones: Mapped[list[str]] = mapped_column(JSONList, default=list, nullable=False)
    emails: Mapped[list[str]] = mapped_column(JSONList, default=list, nullable=False)
    social_links: Mapped[dict[str, str]] = mapped_column(JSONDict, default=dict, nullable=False)
    registry_ids: Mapped[dict[str, str]] = mapped_column(JSONDict, default=dict, nullable=False)
    track: Mapped[str] = mapped_column(String(10), default="unknown", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="new", nullable=False, index=True)
    disqualify_reason: Mapped[str | None] = mapped_column(String(255))
    score_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    score_band: Mapped[str | None] = mapped_column(String(10))
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    scoring_version: Mapped[str | None] = mapped_column(String(32))
    signals: Mapped[list[str]] = mapped_column(JSONList, default=list, nullable=False)
    owner_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("organization.id", ondelete="SET NULL")
    )
    notes: Mapped[str | None] = mapped_column(Text)
    last_engaged_at: Mapped[datetime | None] = mapped_column(DateTime)
    # Phase 2
    lead_source: Mapped[str | None] = mapped_column(String(30), index=True)  # discovery/import/manual/inbound/github/httparchive
    inbound_at: Mapped[datetime | None] = mapped_column(DateTime)  # first inbound (consented) submission
    notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime)  # organisation-level notice (R7)
    tracked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # weekly light re-check
    next_recheck_at: Mapped[datetime | None] = mapped_column(DateTime, index=True)

    websites: Mapped[list[Website]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    place_links: Mapped[list[PlaceLink]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    evidence: Mapped[list[Evidence]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    persons: Mapped[list[Person]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    crm_links: Mapped[list[CrmLink]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )
    notice_events: Mapped[list[NoticeEvent]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint(f"status in {ORG_STATUSES}", name="ck_org_status"),
        CheckConstraint(f"track in {TRACKS}", name="ck_org_track"),
    )

    @property
    def label(self) -> str:
        if self.display_name:
            return self.display_name
        if self.domain:
            return self.domain
        if self.place_links:
            return f"Place {self.place_links[0].place_id[:10]}… (pending enrichment)"
        return f"Organisation #{self.id}"

    @property
    def primary_website(self) -> Website | None:
        return self.websites[0] if self.websites else None

    @property
    def known_contacts(self) -> list[Person]:
        """People from the owner's own LinkedIn connections export linked to this organisation."""
        return [p for p in self.persons if p.source == "linkedin_connections"]


class PlaceLink(Base):
    __tablename__ = "place_link"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(30), default="google_places", nullable=False)
    place_id: Mapped[str] = mapped_column(String(255), nullable=False)
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    coords_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    first_seen_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("discovery_run.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="place_links")

    __table_args__ = (UniqueConstraint("provider", "place_id", name="uq_place_link"),)


class Website(Base, TimestampMixin):
    __tablename__ = "website"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text)
    host_key: Mapped[str | None] = mapped_column(String(255), index=True)
    http_status: Mapped[int | None] = mapped_column(Integer)
    cms: Mapped[str | None] = mapped_column(String(40))
    cms_version: Mapped[str | None] = mapped_column(String(20))
    is_woocommerce: Mapped[bool | None] = mapped_column(Boolean)
    theme: Mapped[str | None] = mapped_column(String(80))
    page_builder: Mapped[str | None] = mapped_column(String(40))
    caching_layer: Mapped[str | None] = mapped_column(String(40))
    cdn: Mapped[str | None] = mapped_column(String(40))
    managed_host: Mapped[str | None] = mapped_column(String(40))
    server_header: Mapped[str | None] = mapped_column(String(120))
    php_version: Mapped[str | None] = mapped_column(String(20))
    host_ip: Mapped[str | None] = mapped_column(String(45))
    host_asn: Mapped[str | None] = mapped_column(String(20))
    host_provider: Mapped[str | None] = mapped_column(String(120))
    host_class: Mapped[str | None] = mapped_column(String(20))
    mail_provider: Mapped[str | None] = mapped_column(String(40))
    tls_valid: Mapped[bool | None] = mapped_column(Boolean)
    tls_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_state: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    error_first_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    robots_allowed: Mapped[bool | None] = mapped_column(Boolean)
    last_probed_at: Mapped[datetime | None] = mapped_column(DateTime)
    probe_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    organization: Mapped[Organization] = relationship(back_populates="websites")
    performance: Mapped[list[PerformanceSnapshot]] = relationship(
        back_populates="website", cascade="all, delete-orphan", order_by="PerformanceSnapshot.id"
    )
    snapshots: Mapped[list[RawSnapshot]] = relationship(
        back_populates="website", cascade="all, delete-orphan"
    )

    __table_args__ = (CheckConstraint(f"error_state in {ERROR_STATES}", name="ck_site_error"),)

    def latest_performance(self, strategy: str = "mobile", source: str | None = "psi") -> PerformanceSnapshot | None:
        """Newest Lighthouse (lab) snapshot by default; `source=None` means any source."""
        rows = [p for p in self.performance if p.strategy == strategy and (source is None or p.source == source)]
        return rows[-1] if rows else None

    def latest_field_performance(self, strategy: str = "mobile") -> PerformanceSnapshot | None:
        """Newest snapshot that carries real-user (CrUX) data, from PageSpeed or the CrUX API."""
        rows = [p for p in self.performance if p.strategy == strategy and p.field_available]
        return rows[-1] if rows else None


class PerformanceSnapshot(Base):
    __tablename__ = "performance_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("website.id", ondelete="CASCADE"), nullable=False, index=True
    )
    strategy: Mapped[str] = mapped_column(String(10), default="mobile", nullable=False)
    lh_performance: Mapped[int | None] = mapped_column(Integer)
    lcp_ms: Mapped[int | None] = mapped_column(Integer)
    tbt_ms: Mapped[int | None] = mapped_column(Integer)
    cls: Mapped[float | None] = mapped_column(Float)
    ttfb_ms: Mapped[int | None] = mapped_column(Integer)
    field_lcp_p75: Mapped[int | None] = mapped_column(Integer)
    field_inp_p75: Mapped[int | None] = mapped_column(Integer)
    field_cls_p75: Mapped[float | None] = mapped_column(Float)
    field_available: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    field_origin_fallback: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="psi", nullable=False)
    runs: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # purged after retention_raw_days
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    website: Mapped[Website] = relationship(back_populates="performance")


class RawSnapshot(Base):
    __tablename__ = "raw_snapshot"

    id: Mapped[int] = mapped_column(primary_key=True)
    website_id: Mapped[int] = mapped_column(
        ForeignKey("website.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), default="html", nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    website: Mapped[Website] = relationship(back_populates="snapshots")


class Evidence(Base):
    """Provenance for every stored fact (R1)."""

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer)
    field: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)

    organization: Mapped[Organization] = relationship(back_populates="evidence")

    __table_args__ = (Index("ix_evidence_org_field", "organization_id", "field"),)


class ConsentRecord(Base):
    __tablename__ = "consent_record"

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("person.id", ondelete="SET NULL"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(120), nullable=False)
    text_version: Mapped[str] = mapped_column(String(40), nullable=False)
    captured_via: Mapped[str] = mapped_column(String(20), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    proof_ref: Mapped[str | None] = mapped_column(String(255))
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime)


class Person(Base, TimestampMixin):
    """Named contacts. Consent-bound by schema (R2)."""

    __tablename__ = "person"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Nullable since Phase 2: a connection from the owner's own LinkedIn export may not match
    # any organisation yet; it is linked when one appears.
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organization.id", ondelete="CASCADE"), index=True
    )
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role_title: Mapped[str | None] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(255), index=True)
    phone_e164: Mapped[str | None] = mapped_column(String(20), index=True)
    linkedin_url: Mapped[str | None] = mapped_column(Text)  # reference only, never crawled
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    lawful_basis: Mapped[str] = mapped_column(String(30), nullable=False)
    lia_reference: Mapped[str | None] = mapped_column(String(255))
    consent_id: Mapped[int | None] = mapped_column(
        ForeignKey("consent_record.id", ondelete="SET NULL", use_alter=True, name="fk_person_consent")
    )
    notice_status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    # Phase 2
    external_id: Mapped[str | None] = mapped_column(String(120), index=True)  # e.g. LinkedIn OIDC `sub`
    picture_url: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)  # source extras (company, connected_on)

    organization: Mapped[Organization | None] = relationship(back_populates="persons")

    __table_args__ = (
        CheckConstraint(f"lawful_basis in {LAWFUL_BASES}", name="ck_person_basis"),
        CheckConstraint(f"notice_status in {NOTICE_STATUSES}", name="ck_person_notice"),
    )

    @property
    def exportable(self) -> bool:
        return not self.do_not_contact and self.notice_status != "objected"

    @property
    def is_known_connection(self) -> bool:
        return self.source == "linkedin_connections"


class Suppression(Base):
    __tablename__ = "suppression"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))
    requested_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("kind", "value_hash", name="uq_suppression"),
        CheckConstraint(f"kind in {SUPPRESSION_KINDS}", name="ck_suppression_kind"),
    )


class DiscoveryRun(Base):
    __tablename__ = "discovery_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    track: Mapped[str] = mapped_column(String(10), default="smb", nullable=False)
    provider: Mapped[str] = mapped_column(String(30), default="google_places", nullable=False)
    query: Mapped[str] = mapped_column(String(255), nullable=False)
    location: Mapped[str | None] = mapped_column(String(255))
    region_code: Mapped[str | None] = mapped_column(String(2))
    max_pages: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    pages_fetched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidates_found: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidates_without_website: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidates_suppressed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    organizations_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    organizations_matched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    calls_made: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_estimate_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    started_by: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ImportBatch(Base):
    __tablename__ = "import_batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    track: Mapped[str] = mapped_column(String(10), default="unknown", nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_ok: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rows_error: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    organizations_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    organizations_matched: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    persons_created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    column_mapping: Mapped[dict[str, str]] = mapped_column(JSONDict, default=dict, nullable=False)
    lawful_basis: Mapped[str | None] = mapped_column(String(30))
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONList, default=list, nullable=False)
    imported_by: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Job(Base):
    __tablename__ = "job"

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)
    scrub_on_done: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ActivityLog(Base):
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    actor_label: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    action: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(30))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict, nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


class ScoringRuleVersion(Base):
    __tablename__ = "scoring_rule_version"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_label: Mapped[str] = mapped_column(String(40), nullable=False)
    checksum: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    yaml_text: Mapped[str] = mapped_column(Text, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ConnectorState(Base):
    """Kill switch, terms-review clock (R10) and daily caps (R11) per connector."""

    __tablename__ = "connector_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_terms_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    terms_url: Mapped[str | None] = mapped_column(Text)
    terms_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    terms_reviewed_by: Mapped[str | None] = mapped_column(String(120))
    daily_cap: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    calls_today: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    calls_date: Mapped[date | None] = mapped_column(Date)
    last_called_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)


# --- Phase 2 ---------------------------------------------------------------------------------
RIGHTS_KINDS = ("access", "erasure", "objection", "correction", "withdraw_consent")
RIGHTS_STATUSES = ("open", "in_progress", "completed", "rejected")
NOTICE_CHANNELS = ("email", "linkedin", "whatsapp", "phone", "letter", "form", "crm")


class InboundSubmission(Base):
    """A consented inbound form submission (website webhook, hosted form or LinkedIn sign-in)."""

    __tablename__ = "inbound_submission"

    id: Mapped[int] = mapped_column(primary_key=True)
    form_key: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    captured_via: Mapped[str] = mapped_column(String(20), nullable=False)  # webhook / hosted_form / linkedin_oidc
    status: Mapped[str] = mapped_column(String(20), default="processed", nullable=False)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id", ondelete="SET NULL"), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"))
    consent_id: Mapped[int | None] = mapped_column(ForeignKey("consent_record.id", ondelete="SET NULL"))
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    ip_hash: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str | None] = mapped_column(Text)  # free-text left by the prospect
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False, index=True)


class RightsRequest(Base):
    """Data-subject request workflow (R6/R7): one row per request, with due date and resolution."""

    __tablename__ = "rights_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)  # identifier as given (email / phone / domain)
    channel: Mapped[str] = mapped_column(String(20), default="email", nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    organization_id: Mapped[int | None] = mapped_column(ForeignKey("organization.id", ondelete="SET NULL"))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    assigned_to: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    resolution: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_by: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_by: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(f"kind in {RIGHTS_KINDS}", name="ck_rights_kind"),
        CheckConstraint(f"status in {RIGHTS_STATUSES}", name="ck_rights_status"),
    )

    @property
    def overdue(self) -> bool:
        return self.status in {"open", "in_progress"} and self.due_at < utcnow()


class NoticeEvent(Base):
    """Record of a privacy notice actually delivered (R7 notice tracking)."""

    __tablename__ = "notice_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("person.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    template_version: Mapped[str] = mapped_column(String(40), nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    sent_by: Mapped[int | None] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)

    organization: Mapped[Organization] = relationship(back_populates="notice_events")

    __table_args__ = (CheckConstraint(f"channel in {NOTICE_CHANNELS}", name="ck_notice_channel"),)


class CrmLink(Base):
    """Mapping between a Prospector entity and its record in the CRM (idempotent upserts)."""

    __tablename__ = "crm_link"

    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(20), nullable=False)  # espocrm
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)  # organization / person
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organization.id", ondelete="CASCADE"), nullable=False, index=True)
    remote_id: Mapped[str] = mapped_column(String(80), nullable=False)
    remote_url: Mapped[str | None] = mapped_column(Text)
    push_hash: Mapped[str | None] = mapped_column(String(64))
    remote_status: Mapped[str | None] = mapped_column(String(60))
    last_pushed_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_pulled_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(Text)

    organization: Mapped[Organization] = relationship(back_populates="crm_links")

    __table_args__ = (UniqueConstraint("target", "entity_type", "entity_id", name="uq_crm_link_entity"),)
