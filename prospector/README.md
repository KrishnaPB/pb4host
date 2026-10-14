# PB4HOST Prospector

Organisation-first lead discovery and enrichment for PB4HOST's WordPress support business.
This is the Phase 1 MVP described in
[`docs/customer-intelligence-app-implementation-plan.md`](../docs/customer-intelligence-app-implementation-plan.md)
and it honours the compliance rules R1–R11 from that plan by construction, not by convention.

**What it does**

1. **Discovers** businesses through Google Places Text Search (`places_text_search` job) — storing only the
   place ID and 30-day coordinates — or through CSV imports and manual research capture.
2. **Enriches** each business from *its own website* (robots-aware, ≤1 req/s/host, passive paths only):
   CMS/WordPress version, WooCommerce, page builder, caching, CDN, PHP header, TLS, visible error states
   (DB error, PHP fatal, maintenance, parked), published phones/generic mailboxes/GSTIN/CIN/social links.
3. Adds **DNS/ASN** facts (hosting provider and class, mail provider) and **PageSpeed Insights** metrics
   (Lighthouse lab + CrUX field data) for WordPress sites and agencies.
4. **Scores** every organisation with versioned YAML rules (`prospector/scoring/v1.yaml`) for two tracks —
   SMB and agency — and bands them hot/warm/cold. Error states only score after a +1 h / +24 h re-check.
5. Gives analysts an **HTMX review UI**: runs, review queue with filters, organisation detail with full evidence,
   live Google attribution panel (never stored), manual capture, imports, suppression/rights handling,
   CSV exports with notice text, connector kill switches, job queue and audit log.

**What it deliberately does not do**

- Automated connectors never create person records (R2); named contacts exist only via manual research with a
  lawful basis, CSV imports with a declared basis (existing customers, the owner's own LinkedIn export), or —
  in Phase 2 — inbound consent flows.
- No LinkedIn scraping or automation; LinkedIn data enters only through the compliant routes above.
- No bulk unsolicited messaging, no SMS/voice telemarketing. Exports carry a notice text for 1:1 outreach.
- No Google Places content beyond place ID + coordinates is persisted (R3); the detail panel is live-only.

## Quick start (local, SQLite, no API keys)

```bash
cd prospector
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # edit SECRET_KEY / SUPPRESSION_SALT at minimum
prospector init-db
prospector create-user --email you@example.com --name "You" --role owner   # prompts for a password
prospector demo-seed            # optional: synthetic "Demo:" organisations on .example domains
prospector serve                # http://localhost:8000
prospector worker               # in a second terminal
```

Run the tests: `pytest -q` (57 tests: unit, connector contracts with mocked HTTP, compliance R1–R11, API, and an
end-to-end pipeline through the job queue).

## Turning on real data sources

| Connector | Needs | Where |
|---|---|---|
| `google_places` | `GOOGLE_MAPS_API_KEY` (Places API (New) enabled, key restricted to it) and an owner clicking **Mark terms reviewed** in *Admin → Connectors* | Text Search costs one call per page of ≤20 results; default cap 200 calls/day |
| `website_probe` | nothing — but publish the crawler policy page (`docs/crawler-policy.md`) at `CRAWLER_POLICY_URL` | passive crawl of the business's own site |
| `dns_asn` | owner terms review (Team Cymru IP-to-ASN DNS service) | free |
| `pagespeed` | owner terms review; `PSI_API_KEY` recommended (25k queries/day) | free |
| `csv_import` | nothing | analyst role |

Every connector has an enable/disable switch, a daily cap, and a 90-day terms review timer (R10). Calls stop with a
clear error when the cap is reached and resume automatically the next day.

## Week-2 validation spike

```bash
prospector spike urls.txt --out spike-report.json
```

Probes each URL (one line per site; hints with paths or tracking parameters are reduced to the site root), scores
it and writes a report with empty `human_review` fields. Fill in `cms_correct` / `contact_correct` for 50 sites to
measure precision against the plan's targets (≥90 % CMS, ≥85 % contact facts). Add `--pagespeed` once that
connector is reviewed. Agency signals promote `--track unknown` sites to the agency track automatically.

The crawler refuses loopback, private, link-local and reserved addresses (SSRF guard) — set
`CRAWLER_ALLOW_PRIVATE_HOSTS=true` only for a local dry run against fixture sites, never in production.

## Deployment (PB4HOST VPS)

`infra/docker-compose.yml` runs PostgreSQL 16, the web app, one worker, Caddy (automatic TLS, security headers,
`/metrics` blocked from the internet) and a nightly `pg_dump` sidecar.

```bash
cp .env.example .env   # set SECRET_KEY, SUPPRESSION_SALT, POSTGRES_PASSWORD, PROSPECTOR_HOST, ADMIN_EMAIL, API keys
cd infra && docker compose up -d --build
docker compose exec web prospector create-user --email owner@pb4host.com --name Owner --role owner
```

Operations, rights requests, incident steps and restore procedure: [`docs/runbook.md`](docs/runbook.md).

## Layout

```
prospector/
  api/            FastAPI app, routers, Jinja templates (Pico CSS + htmx, vendored), static assets
  compliance/     storage policies, audit log, suppression & rights, retention
  connectors/     google_places, website_probe/, dns_asn, pagespeed, csv_import (+ base registry/guards)
  core/           normalisation, facts→evidence, dedupe, scoring engine, organisation helpers
  jobs/           DB-backed queue, task handlers, worker loop
  scoring/        v1.yaml rules (checksum = scoring version)
  cli.py          init-db · migrate · create-user · serve · worker · run · spike · retention · demo-seed
migrations/       Alembic (0001_initial_schema)
tests/            unit · contract · compliance · api · pipeline, with HTML/JSON fixtures
infra/            Dockerfile, docker-compose.yml, Caddyfile, backup.sh
docs/             runbook, crawler policy
```

## Roles

- **viewer** — read-only access to runs, organisations and exports pages.
- **analyst** — everything an operator needs: runs, status changes, capture, imports, exports, suppressions, rights lookup.
- **owner** — plus connectors, users/2FA, audit log, retention trigger and erasure.

Licence: GPL-3.0-or-later (same as the rest of this repository). htmx (0BSD) and Pico CSS (MIT) are vendored
under `prospector/api/static/vendor/` with their licences.
