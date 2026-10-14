# Prospector runbook

Audience: whoever operates the PB4HOST Prospector VPS. Keep this next to the `.env` secrets (not in Git).

## 1. Daily / weekly checks

| When | Check | Where |
|---|---|---|
| Daily | Queue is draining (no `failed` pile-up, `queued` not growing) | `/admin/jobs`, `/metrics` (`prospector_jobs{status=...}`) |
| Daily | Connector call counts against caps; `last_error` empty | `/admin/connectors` |
| Daily | Review queue: triage `new` hot/warm leads → `reviewing` / `qualified` / `disqualified` | `/organizations?status=new` |
| Weekly | Export qualified leads with notice text, mark as exported | `/exports` |
| Weekly | Retention report ran (`retention.run` in audit log) and backups exist (`docker compose exec backup ls /backups`) | `/admin/audit?action=retention.` |
| Every 90 days | Terms review per connector (Google Maps Platform, PSI, Team Cymru) — read the terms, then **Mark terms reviewed** | `/admin/connectors` |
| Quarterly | Rotate `API_TOKEN`; review users; confirm 2FA on owner accounts | `/admin/users` |

## 2. Jobs and the worker

- Queue: table `job`. Types: `places_text_search`, `website_probe`, `confirm_error_state`, `dns_asn`, `pagespeed`,
  `score_organization`, `retention_purge`. Idempotency keys prevent duplicates per day.
- Retry policy: 3 attempts, back-off 5 min × attempt. `CapExceeded` re-schedules for 00:05 UTC next day without
  consuming an attempt. `ConnectorDisabled` / `TermsReviewRequired` fail permanently (re-run after fixing).
- Retry a failed job: `/admin/jobs` → **Retry**. Drain once from a shell: `prospector worker --once`.
- Worker logs are redacted (e-mail addresses and phone-like strings replaced) — do not add raw PII to log lines.
- Finished jobs that carried transient Places data have their payload scrubbed (`"scrubbed": true`).

## 3. Rights requests (access / erasure / objection) — target: 7 days, legal max per DPDP rules

1. `/suppressions` → **Rights request lookup** with the domain, e-mail, phone, name or place ID the requester gave.
2. Access request: **Export JSON** for each matched organisation and send it to the requester.
3. Erasure / objection: owner clicks **Erase** on the organisation (anonymises, deletes contacts + evidence, keeps
   non-identifying technical history, adds hashed identifiers to the suppression list) or on a single contact.
4. Note the request date and completion in the audit log — it is recorded automatically as `rights.lookup`,
   `rights.export`, `organization.anonymise`, `person.erase`, `suppression.add`.
5. Suppression hashes are kept indefinitely so a later discovery run cannot re-create the record.

## 4. Kill switches

- A connector misbehaves or a provider objects: `/admin/connectors` → **Disable**. Running jobs finish; queued ones fail
  with `ConnectorDisabled` and can be retried later.
- Entire crawler: disable `website_probe`. Discovery still records place IDs; nothing is fetched.
- Emergency: `docker compose stop worker` halts all outbound traffic from the app in seconds.

## 5. Website owner complaints ("stop crawling me")

1. Add the domain to the suppression list (`/suppressions`, kind `domain`). Discovery and exports skip it; the crawler
   checks suppression before probing.
2. If they ask for deletion, run the erasure flow (section 3).
3. Reply with the crawler policy page URL (`docs/crawler-policy.md`, published at `CRAWLER_POLICY_URL`).

## 6. Incident: suspected data breach

1. `docker compose stop web worker` — contain.
2. Preserve logs: `docker compose logs --no-color > incident-$(date +%F).log` (already redacted).
3. Rotate `SECRET_KEY` (logs everyone out), `API_TOKEN`, API keys in Google Cloud, `POSTGRES_PASSWORD`.
4. Assess what was exposed using `/admin/audit` and the `person` table (counts by lawful basis).
5. DPDP Rules 2025: notify the Data Protection Board and affected individuals; the detailed report is due within
   72 hours of becoming aware. Record timings. Get counsel involved at this step.
6. Restore from backup if integrity is in doubt (section 8).

## 7. Costs and quotas

- Google Places: `places_daily_page_cap` (default 200 pages/day ≈ 4,000 candidates). Check the billing dashboard
  weekly; the monthly budget decision is ₹5,000. Text Search with the website field triggers a Pro/Enterprise SKU —
  verify the SKU in the Cloud console after the first run and adjust `PLACES_PAGES_PER_RUN`.
- PageSpeed: 25,000 queries/day with a key; the app caps at 2,000.
- Team Cymru DNS: free for non-bulk use; cap 5,000/day; be prepared to switch to a paid IP-intelligence feed
  if volume grows.

## 8. Backups and restore

- `backup` sidecar dumps PostgreSQL nightly to the `backups` volume (14 days kept) plus a tarball of `/data`
  (gzipped raw HTML snapshots, themselves purged after 30 days).
- Restore: `gunzip -c prospector-YYYYMMDD-HHMM.sql.gz | docker compose exec -T db psql -U prospector prospector`,
  then `docker compose restart web worker`.
- Test a restore quarterly on a scratch database.

## 9. Upgrades

```bash
git pull
cd infra && docker compose build && docker compose run --rm migrate && docker compose up -d
```

Scoring rule changes: edit `prospector/scoring/v1.yaml`, redeploy, then **Re-score** affected organisations
(the YAML checksum is stored with every score so old and new scores are distinguishable).

## 10. Local development notes

- SQLite is the default (`DATABASE_URL=sqlite:///./data/prospector.db`); `prospector init-db` creates tables and stamps
  Alembic. PostgreSQL uses `prospector migrate` (Alembic `0001_initial_schema`).
- `prospector demo-seed` creates clearly synthetic organisations (`Demo:` prefix, `.example` domains, source
  `demo_seed`) so the UI can be explored without any external call. Delete them with
  `DELETE FROM organization WHERE domain LIKE '%.example'` or just re-create the dev database.
- Tests never touch the network: `pytest -q`.
