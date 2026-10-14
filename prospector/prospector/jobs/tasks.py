"""Job handlers: discovery → probe → DNS → PageSpeed → score, plus re-checks and retention."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..compliance import audit
from ..compliance.retention import run_retention
from ..compliance.suppression import is_suppressed, organization_is_suppressed
from ..config import get_settings
from ..connectors.base import Connector, ConnectorError
from ..connectors.companies_house import CompaniesHouseConnector
from ..connectors.crux import CruxConnector
from ..connectors.dns_asn import DnsAsnConnector
from ..connectors.github_orgs import GithubOrgsConnector
from ..connectors.google_places import GooglePlacesConnector
from ..connectors.httparchive_feed import HttpArchiveFeedConnector
from ..connectors.linkedin_connections import link_known_contacts
from ..connectors.pagespeed import PageSpeedConnector
from ..connectors.website_probe.probe import WebsiteProbeConnector, write_snapshot
from ..core.facts import Fact, apply_facts
from ..core.normalize import hosted_profile_network, registered_domain
from ..core.organizations import ensure_website, resolve_organization
from ..core.scoring import score_organization as _score
from ..models import (
    CrmLink,
    DiscoveryRun,
    Organization,
    PerformanceSnapshot,
    PlaceLink,
    RawSnapshot,
    Website,
)
from ..util import utcnow
from .queue import enqueue, handler

log = logging.getLogger(__name__)

# Tests and the CLI can inject pre-configured connector instances here.
overrides: dict[str, Connector] = {}


def connector(name: str) -> Any:
    if name in overrides:
        return overrides[name]
    return {
        "google_places": GooglePlacesConnector,
        "website_probe": WebsiteProbeConnector,
        "dns_asn": DnsAsnConnector,
        "pagespeed": PageSpeedConnector,
        "crux": CruxConnector,
        "github_orgs": GithubOrgsConnector,
        "httparchive_feed": HttpArchiveFeedConnector,
        "companies_house": CompaniesHouseConnector,
    }[name]()


def enqueue_enrichment(session: Session, org_id: int, url_hint: str, *, country: str | None = None, reason: str = "discovery",
                       force_pagespeed: bool = False, light: bool = False) -> None:
    today = utcnow().strftime("%Y%m%d")
    enqueue(
        session,
        "website_probe",
        {"organization_id": org_id, "url_hint": url_hint, "country": country, "reason": reason,
         "force_pagespeed": force_pagespeed, "light": light, "homepage_only": light},
        idempotency_key=f"probe:{org_id}:{today}:{reason}",
        scrub_on_done=True,  # the hint may originate from Places content (R3)
    )


def enqueue_inbound_followups(session: Session, org: Organization) -> None:
    """An inbound lead is enriched immediately and pushed to the CRM (acceptance: visible within 10 minutes)."""
    site = org.primary_website
    if site is not None:
        enqueue_enrichment(session, org.id, site.url, country=org.country, reason=f"inbound{utcnow():%H%M%S}", force_pagespeed=True)
    enqueue(session, "crm_sync", {"organization_id": org.id, "reason": "inbound"},
            idempotency_key=f"crm:{org.id}:{utcnow():%Y%m%d%H%M}", run_after=utcnow() + timedelta(minutes=3))


def enqueue_crm_sync(session: Session, org_id: int, *, reason: str = "manual", force: bool = False) -> None:
    enqueue(session, "crm_sync", {"organization_id": org_id, "reason": reason, "force": force},
            idempotency_key=f"crm:{org_id}:{utcnow():%Y%m%d%H%M%S}:{reason}")


# --- discovery -------------------------------------------------------------------------------
@handler("places_text_search")
def places_text_search(session: Session, payload: dict[str, Any]) -> None:
    run = session.get(DiscoveryRun, payload["run_id"])
    if run is None or run.status in {"cancelled", "failed"}:
        return
    page_no = int(payload.get("page_no", 1))
    if run.status == "queued":
        run.status = "running"
        run.started_at = utcnow()
        session.commit()
    places = connector("google_places")
    try:
        page = places.search_text(
            session, run.query if not run.location else f"{run.query} in {run.location}",
            track=run.track, region_code=run.region_code, page_token=payload.get("page_token"),
        )
    except ConnectorError as exc:
        run.status = "failed"
        run.error = str(exc)[:1000]
        run.finished_at = utcnow()
        session.commit()
        raise
    run.pages_fetched += 1
    run.calls_made += 1
    run.cost_estimate_usd += places.cost_per_call_usd
    run.candidates_found += page.raw_count
    run.candidates_without_website += page.without_website
    for cand in page.candidates:
        if hosted_profile_network(cand.website_hint):
            # a Facebook page / Linktree / marketplace listing is not an own website: nothing to
            # enrich from, and Google content itself may not be stored (R3)
            run.candidates_without_website += 1
            continue
        if is_suppressed(session, "place_id", cand.external_id) or is_suppressed(session, "domain", cand.website_hint or ""):
            run.candidates_suppressed += 1
            continue
        track = cand.track
        if track == "smb" and cand.transient.get("agency_type_hint"):
            track = "agency"
        resolved = resolve_organization(session, website=cand.website_hint, place_id=cand.external_id, track=track)
        org = resolved.organization
        if resolved.created:
            run.organizations_created += 1
        else:
            run.organizations_matched += 1
        link = next((pl for pl in org.place_links if pl.place_id == cand.external_id), None)
        if link is None:
            link = PlaceLink(organization=org, provider=cand.provider, first_seen_run_id=run.id, **cand.persist)
            session.add(link)
        else:
            link.last_seen_at = utcnow()
            link.lat = cand.persist.get("lat")
            link.lng = cand.persist.get("lng")
            link.coords_expires_at = cand.persist.get("coords_expires_at")
        session.flush()
        if cand.website_hint:
            enqueue_enrichment(session, org.id, cand.website_hint, reason=f"run{run.id}")
    session.commit()
    if page.next_page_token and page_no < run.max_pages:
        enqueue(
            session,
            "places_text_search",
            {"run_id": run.id, "page_token": page.next_page_token, "page_no": page_no + 1},
            idempotency_key=f"run:{run.id}:page:{page_no + 1}",
            scrub_on_done=True,
        )
    else:
        run.status = "done"
        run.finished_at = utcnow()
    session.commit()


# --- enrichment ------------------------------------------------------------------------------
@handler("website_probe")
def website_probe(session: Session, payload: dict[str, Any]) -> None:
    settings = get_settings()
    org = session.get(Organization, payload["organization_id"])
    if org is None or org.status in {"merged", "anonymised", "suppressed"}:
        return
    if organization_is_suppressed(session, org):
        org.status = "suppressed"
        return
    url_hint = payload.get("url_hint") or org.website_url
    if not url_hint:
        return
    probe = connector("website_probe")
    light = bool(payload.get("light"))
    result = probe.probe(session, url_hint, country_hint=org.country, homepage_only=bool(payload.get("homepage_only")) or light)
    site = ensure_website(session, org, result.final_url or result.requested_url)
    site.robots_allowed = not result.robots_blocked
    site.last_probed_at = utcnow()
    site.probe_count += 1
    previous_state = site.error_state
    apply_facts(session, org, result.facts, site)
    if result.signals:
        org.signals = list(dict.fromkeys([*org.signals, *result.signals]))
        if org.track == "unknown" and "agency_keywords" in result.signals:
            apply_facts(session, org, [Fact("organization.track", "agency", "derived", result.final_url or url_hint, 0.6)], site)
    # Error-state confirmation lifecycle
    if result.error_state != "none":
        if previous_state != result.error_state or site.error_first_seen_at is None:
            site.error_first_seen_at = utcnow()
            site.error_confirmed_at = None
            for minutes in settings.error_recheck_delays_minutes:
                enqueue(
                    session,
                    "confirm_error_state",
                    {"organization_id": org.id, "website_id": site.id, "expected_state": result.error_state},
                    idempotency_key=f"recheck:{site.id}:{site.error_first_seen_at:%Y%m%d%H%M}:{minutes}",
                    run_after=utcnow() + timedelta(minutes=minutes),
                )
    else:
        site.error_first_seen_at = None
        site.error_confirmed_at = None
    for source_url, html in result.snapshots:
        path, expires = write_snapshot(settings.data_dir, site.id, source_url, html, settings.retention_raw_days)
        session.add(RawSnapshot(website=site, kind="html", path=path, source_url=source_url, expires_at=expires))
    session.flush()
    link_known_contacts(session, org)  # owner's LinkedIn connections whose company matches
    if result.reachable and site.host_key:
        enqueue(session, "dns_asn", {"organization_id": org.id, "website_id": site.id},
                idempotency_key=f"dns:{site.id}:{utcnow():%Y%m%d}")
        wants_psi = bool(payload.get("force_pagespeed")) or site.cms == "wordpress" or org.track == "agency"
        if light and wants_psi:
            last = site.latest_performance("mobile")
            wants_psi = last is None or (utcnow() - last.fetched_at).days >= settings.recheck_psi_after_days
        if wants_psi:
            for strategy in settings.psi_strategies:
                enqueue(session, "pagespeed", {"organization_id": org.id, "website_id": site.id, "strategy": strategy},
                        idempotency_key=f"psi:{site.id}:{strategy}:{utcnow():%Y%m%d}")
        if not light:
            enqueue(session, "crux_origin", {"organization_id": org.id, "website_id": site.id},
                    idempotency_key=f"crux:{site.id}:{utcnow():%Y%m}")
    enqueue(session, "score_organization", {"organization_id": org.id},
            idempotency_key=f"score:{org.id}:{utcnow():%Y%m%d%H%M%S}")


@handler("confirm_error_state")
def confirm_error_state(session: Session, payload: dict[str, Any]) -> None:
    org = session.get(Organization, payload["organization_id"])
    site = session.get(Website, payload["website_id"])
    if org is None or site is None or org.status in {"merged", "anonymised", "suppressed"}:
        return
    probe = connector("website_probe")
    result = probe.probe(session, site.final_url or site.url, country_hint=org.country, homepage_only=True)
    if result.error_state == payload.get("expected_state") and result.error_state != "none":
        site.error_confirmed_at = utcnow()
        apply_facts(session, org, [f for f in result.facts if f.field in {"website.error_state", "website.http_status"}], site)
    elif result.error_state == "none":
        site.error_state = "none"
        site.error_first_seen_at = None
        site.error_confirmed_at = None
    else:
        site.error_state = result.error_state  # changed state; restart confirmation
        site.error_first_seen_at = utcnow()
        site.error_confirmed_at = None
    session.flush()
    enqueue(session, "score_organization", {"organization_id": org.id},
            idempotency_key=f"score:{org.id}:{utcnow():%Y%m%d%H%M%S}:recheck")


@handler("dns_asn")
def dns_asn(session: Session, payload: dict[str, Any]) -> None:
    org = session.get(Organization, payload["organization_id"])
    site = session.get(Website, payload["website_id"])
    if org is None or site is None or not site.host_key:
        return
    dns = connector("dns_asn")
    result = dns.lookup(session, site.host_key, registered_domain(site.host_key))
    apply_facts(session, org, dns.facts(result, f"dns://{site.host_key}"), site)
    enqueue(session, "score_organization", {"organization_id": org.id},
            idempotency_key=f"score:{org.id}:{utcnow():%Y%m%d%H%M%S}:dns")


@handler("pagespeed")
def pagespeed(session: Session, payload: dict[str, Any]) -> None:
    org = session.get(Organization, payload["organization_id"])
    site = session.get(Website, payload["website_id"])
    if org is None or site is None:
        return
    url = site.final_url or site.url
    psi = connector("pagespeed")
    result = psi.run(session, url, strategy=payload.get("strategy", "mobile"))
    snap = PerformanceSnapshot(
        website=site,
        strategy=result.strategy,
        lh_performance=result.lh_performance,
        lcp_ms=result.lcp_ms,
        tbt_ms=result.tbt_ms,
        cls=result.cls,
        ttfb_ms=result.ttfb_ms,
        field_lcp_p75=result.field_lcp_p75,
        field_inp_p75=result.field_inp_p75,
        field_cls_p75=result.field_cls_p75,
        field_available=result.field_available,
        field_origin_fallback=result.field_origin_fallback,
        source="psi",
        runs=result.runs,
        raw_json=result.raw,
    )
    session.add(snap)
    session.flush()
    enqueue(session, "score_organization", {"organization_id": org.id},
            idempotency_key=f"score:{org.id}:{utcnow():%Y%m%d%H%M%S}:psi")


@handler("score_organization")
def score_organization(session: Session, payload: dict[str, Any]) -> None:
    org = session.get(Organization, payload["organization_id"])
    if org is None or org.status in {"merged", "anonymised"}:
        return
    session.refresh(org)
    _score(session, org)


@handler("crux_origin")
def crux_origin(session: Session, payload: dict[str, Any]) -> None:
    """Real-user field data straight from the CrUX API (no Lighthouse run, cheap)."""
    org = session.get(Organization, payload["organization_id"])
    site = session.get(Website, payload["website_id"])
    if org is None or site is None or not site.host_key:
        return
    settings = get_settings()
    if not (settings.crux_api_key or settings.psi_api_key):
        return  # not configured: silently skip, PSI still carries field data when it runs
    crux = connector("crux")
    origin = (site.final_url or site.url).split("/", 3)
    origin = f"{origin[0]}//{origin[2]}" if len(origin) >= 3 else site.final_url or site.url
    result = crux.query(session, origin, strategy=payload.get("strategy", "mobile"))
    if not result.available:
        return
    session.add(PerformanceSnapshot(
        website=site, strategy=result.strategy, field_lcp_p75=result.lcp_p75, field_inp_p75=result.inp_p75,
        field_cls_p75=result.cls_p75, ttfb_ms=None, field_available=True, field_origin_fallback=True, source="crux_api",
        runs=1, raw_json=result.raw,
    ))
    session.flush()
    enqueue(session, "score_organization", {"organization_id": org.id},
            idempotency_key=f"score:{org.id}:{utcnow():%Y%m%d%H%M%S}:crux")


# --- Phase 2 discovery sources -----------------------------------------------------------------
def _finish_run(run: DiscoveryRun) -> None:
    run.status = "done"
    run.finished_at = utcnow()


@handler("github_org_search")
def github_org_search(session: Session, payload: dict[str, Any]) -> None:
    run = session.get(DiscoveryRun, payload["run_id"])
    if run is None or run.status in {"cancelled", "failed"}:
        return
    page_no = int(payload.get("page_no", 1))
    if run.status == "queued":
        run.status, run.started_at = "running", utcnow()
        session.commit()
    github = connector("github_orgs")
    try:
        page = github.search_page(session, location=run.location or run.query, page=page_no, extra_query=run.query if run.location else None)
    except ConnectorError as exc:
        run.status, run.error, run.finished_at = "failed", str(exc)[:1000], utcnow()
        session.commit()
        raise
    run.pages_fetched += 1
    run.calls_made += page.calls
    run.candidates_found += page.raw_count
    run.candidates_without_website += page.without_website
    for cand in page.candidates:
        if hosted_profile_network(cand.website_hint):
            run.candidates_without_website += 1
            continue
        if is_suppressed(session, "domain", cand.website_hint or ""):
            run.candidates_suppressed += 1
            continue
        t = cand.transient
        resolved = resolve_organization(session, website=cand.website_hint, name=t.get("name"), city=t.get("city"), track="agency",
                                        lead_source="github")
        org = resolved.organization
        run.organizations_created += int(resolved.created)
        run.organizations_matched += int(not resolved.created)
        ref = t["html_url"]
        facts = [Fact("organization.social", f"github={ref}", "github_api", ref, 1.0)]
        if t.get("name") and not org.display_name:
            facts.append(Fact("organization.display_name", t["name"], "github_api", ref, 0.7))
        if t.get("city") and not org.city:
            facts.append(Fact("organization.city", t["city"], "github_api", ref, 0.6))
        if org.track == "unknown":
            facts.append(Fact("organization.track", "agency", "github_api", ref, 0.7))
        apply_facts(session, org, facts)
        signals = ["github_org"] + (["github_wordpress_repos"] if (t.get("wordpress_repos") or 0) > 0 else [])
        org.signals = list(dict.fromkeys([*org.signals, *signals]))
        org.lead_source = org.lead_source or "github"
        session.flush()
        if cand.website_hint:
            enqueue_enrichment(session, org.id, cand.website_hint, reason=f"run{run.id}")
    session.commit()
    if page.has_more and page_no < run.max_pages:
        enqueue(session, "github_org_search", {"run_id": run.id, "page_no": page_no + 1},
                idempotency_key=f"run:{run.id}:page:{page_no + 1}")
    else:
        _finish_run(run)
    session.commit()


@handler("httparchive_feed_run")
def httparchive_feed_run(session: Session, payload: dict[str, Any]) -> None:
    run = session.get(DiscoveryRun, payload["run_id"])
    if run is None or run.status in {"cancelled", "failed"}:
        return
    run.status, run.started_at = "running", utcnow()
    session.commit()
    feed = connector("httparchive_feed")
    try:
        result = feed.fetch(session, month=payload["month"], country=payload.get("country"), limit=payload.get("limit"))
    except ConnectorError as exc:
        run.status, run.error, run.finished_at = "failed", str(exc)[:1000], utcnow()
        session.commit()
        raise
    run.pages_fetched = 1
    run.calls_made = 1
    run.cost_estimate_usd = result.cost_estimate_usd
    run.candidates_found = len(result.rows)
    for cand in result.candidates():
        if is_suppressed(session, "domain", cand.website_hint or ""):
            run.candidates_suppressed += 1
            continue
        resolved = resolve_organization(session, website=cand.website_hint, track="smb", lead_source="httparchive")
        org = resolved.organization
        run.organizations_created += int(resolved.created)
        run.organizations_matched += int(not resolved.created)
        site = ensure_website(session, org, cand.website_hint or "")
        t = cand.transient
        session.add(PerformanceSnapshot(
            website=site, strategy="mobile", field_lcp_p75=t.get("lcp_p75"), field_inp_p75=t.get("inp_p75"),
            field_cls_p75=t.get("cls_p75"), field_available=True, field_origin_fallback=True, source="crux_bq", runs=1,
            raw_json={"month": t.get("month"), "country": t.get("country"), "rank": t.get("rank")},
        ))
        org.signals = list(dict.fromkeys([*org.signals, "httparchive_wordpress", "crux_poor_lcp"]))
        org.lead_source = org.lead_source or "httparchive"
        session.flush()
        enqueue_enrichment(session, org.id, cand.website_hint or "", reason=f"run{run.id}")
    _finish_run(run)
    session.commit()


# --- CRM -----------------------------------------------------------------------------------------
@handler("crm_sync")
def crm_sync(session: Session, payload: dict[str, Any]) -> None:
    from ..api.routers.exports import notice_text
    from ..crm.sync import build_adapter, push_organization

    settings = get_settings()
    org = session.get(Organization, payload["organization_id"])
    if org is None:
        return
    if not settings.crm_configured:
        log.info("crm_sync skipped for org %s: CRM not configured", org.id)
        return
    adapter = overrides.get("crm_adapter") or build_adapter(settings)
    outcome = push_organization(session, org, adapter, notice_text=notice_text(org), settings=settings, force=bool(payload.get("force")))
    log.info("crm_sync org %s: %s (%s contacts)", org.id, outcome.reason, len(outcome.contacts))


@handler("crm_pull_statuses")
def crm_pull_statuses(session: Session, payload: dict[str, Any]) -> None:
    from ..crm.sync import build_adapter, pull_status

    settings = get_settings()
    if not settings.crm_configured:
        return
    adapter = overrides.get("crm_adapter") or build_adapter(settings)
    limit = int(payload.get("limit", 500))
    links = session.scalars(select(CrmLink).where(CrmLink.entity_type == "organization", CrmLink.remote_id != "")
                            .order_by(CrmLink.last_pulled_at.asc().nullsfirst()).limit(limit)).all()
    for link in links:
        org = session.get(Organization, link.organization_id)
        if org is None or org.status in {"merged", "anonymised"}:
            continue
        try:
            pull_status(session, org, adapter, settings=settings)
        except ConnectorError as exc:
            link.last_error = str(exc)[:500]
        session.commit()


@handler("crm_delete")
def crm_delete(session: Session, payload: dict[str, Any]) -> None:
    """Erasure propagation to the CRM; retried by the queue when the CRM is unreachable."""
    from ..crm.sync import build_adapter

    settings = get_settings()
    if not settings.crm_configured:
        log.warning("crm_delete: CRM not configured; remote %s %s must be removed by hand", payload.get("remote_entity"), payload.get("remote_id"))
        return
    adapter = overrides.get("crm_adapter") or build_adapter(settings)
    removed = adapter.delete_record(payload["remote_entity"], payload["remote_id"])
    audit.log(session, "crm.delete", actor=None, entity_type="crm", remote_entity=payload["remote_entity"], remote_id=payload["remote_id"], removed=removed)


# --- scheduled re-checks ---------------------------------------------------------------------
@handler("schedule_rechecks")
def schedule_rechecks(session: Session, payload: dict[str, Any]) -> int:
    """Weekly light re-probe for tracked organisations (customers, qualified, contacted). Returns the number queued."""
    settings = get_settings()
    now = utcnow()
    due = session.scalars(select(Organization).where(Organization.tracked.is_(True), Organization.next_recheck_at <= now,
                                                     Organization.status.notin_(("merged", "anonymised", "suppressed")))
                          .order_by(Organization.next_recheck_at).limit(int(payload.get("limit", settings.recheck_daily_limit)))).all()
    for org in due:
        site = org.primary_website
        if site is None or not (site.final_url or site.url):
            org.next_recheck_at = now + timedelta(days=settings.recheck_interval_days)
            continue
        enqueue_enrichment(session, org.id, site.final_url or site.url, country=org.country, reason=f"recheck{now:%Y%m%d}", light=True)
        org.next_recheck_at = now + timedelta(days=settings.recheck_interval_days)
    session.commit()
    log.info("scheduled %s re-checks", len(due))
    return len(due)


@handler("retention_purge")
def retention_purge(session: Session, payload: dict[str, Any]) -> None:
    report = run_retention(session)
    log.info("retention report: %s", report)


@handler("noop")
def noop(session: Session, payload: dict[str, Any]) -> None:
    audit.log(session, "noop", actor_label="system")
