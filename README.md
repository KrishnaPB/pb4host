# PB4HOST

## Website audit and organic promotion
Prepared on 13 October 2026 from public website content:

- [Website audit and prioritized action plan](docs/site-audit-2026-10-13.md)
- [Free-channel promotion kit and 30-day calendar](docs/free-promotion-kit.md)
- [Promotion tracking template](docs/promotion-tracker.csv)

The audit distinguishes observed content issues from technical checks still needed. No live WordPress changes or social posts have been made. This repository does not currently include the deployed website source. Keep credentials and customer data out of Git.

## Customer intelligence app ("Prospector")
Plan prepared on 14 October 2026; the Phase 1 MVP now lives in [`prospector/`](prospector/) (no data collection happens until API keys are configured and an owner reviews each connector's terms):

- [Prospector application](prospector/README.md) — FastAPI + worker + PostgreSQL/SQLite, HTMX review UI, connectors for Google Places (place ID + coordinates only), the business's own website, DNS/ASN, PageSpeed Insights and CSV imports; versioned scoring rules; suppression and rights handling; retention jobs; docker-compose deployment with Caddy; [runbook](prospector/docs/runbook.md) and [crawler policy](prospector/docs/crawler-policy.md). Run `pytest -q` inside `prospector/` for the offline test suite.
- [Implementation plan](docs/customer-intelligence-app-implementation-plan.md) — organisation-first lead discovery and enrichment from permitted public sources (Google Places, company websites, PageSpeed Insights, CrUX/HTTP Archive, registries, GitHub, first-party forms), with consent-bound person records, provenance on every field, scoring, and CRM export. LinkedIn is used only through compliant routes (Sign In with LinkedIn, the owner's own export, manual research); no scraping.
- [Prospecting source register](docs/prospecting-source-register.csv) — per-source access route, cost, terms constraints, decision, phase and review dates; the Phase 0 sign-off artefact.
