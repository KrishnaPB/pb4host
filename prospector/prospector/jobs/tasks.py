"""Job handlers: discovery → probe → DNS → PageSpeed → score, plus re-checks and retention."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..compliance import audit
from ..compliance.retention import run_retention
from ..compliance.suppression import is_suppressed, organization_is_suppressed
from ..config import get_settings
from ..connectors.base import Connector, ConnectorError
from ..connectors.dns_asn import DnsAsnConnector
from ..connectors.google_places import GooglePlacesConnector
from ..connectors.pagespeed import PageSpeedConnector
from ..connectors.website_probe.probe import WebsiteProbeConnector, write_snapshot
from ..core.facts import Fact, apply_facts
from ..core.normalize import registered_domain
from ..core.organizations import ensure_website, resolve_organization
from ..core.scoring import score_organization as _score
from ..models import (
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
    }[name]()


def enqueue_enrichment(session: Session, org_id: int, url_hint: str, *, country: str | None = None, reason: str = "discovery") -> None:
    today = utcnow().strftime("%Y%m%d")
    enqueue(
        session,
        "website_probe",
        {"organization_id": org_id, "url_hint": url_hint, "country": country, "reason": reason},
        idempotency_key=f"probe:{org_id}:{today}:{reason}",
        scrub_on_done=True,  # the hint may originate from Places content (R3)
    )


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
    result = probe.probe(session, url_hint, country_hint=org.country, homepage_only=bool(payload.get("homepage_only")))
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
    if result.reachable and site.host_key:
        enqueue(session, "dns_asn", {"organization_id": org.id, "website_id": site.id},
                idempotency_key=f"dns:{site.id}:{utcnow():%Y%m%d}")
        if site.cms == "wordpress" or org.track == "agency":
            for strategy in settings.psi_strategies:
                enqueue(session, "pagespeed", {"organization_id": org.id, "website_id": site.id, "strategy": strategy},
                        idempotency_key=f"psi:{site.id}:{strategy}:{utcnow():%Y%m%d}")
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


@handler("retention_purge")
def retention_purge(session: Session, payload: dict[str, Any]) -> None:
    report = run_retention(session)
    log.info("retention report: %s", report)


@handler("noop")
def noop(session: Session, payload: dict[str, Any]) -> None:
    audit.log(session, "noop", actor_label="system")
