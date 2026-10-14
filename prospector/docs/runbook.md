# Prospector runbook

Audience: whoever operates the PB4HOST Prospector VPS. Keep this next to the `.env` secrets (not in Git).

## 1. Daily / weekly checks

| When | Check | Where |
|---|---|---|
| Daily | Queue is draining (no `failed` pile-up, `queued` not growing) | `/admin/jobs`, `/metrics` (`prospector_jobs{status=...}`) |
| Daily | Connector call counts against caps; `last_error` empty | `/admin/connectors` |
| Daily | Review queue: triage `new` hot/warm leads → `reviewing` / `qualified` / `disqualified` | `/organizations?status=new` |
| Weekly | Export qualified leads with notice text, mark as exported — or let the CRM sync push them | `/exports`, organisation → **CRM → Push now** |
| Daily | Inbound submissions processed (none in `error`), new inbound leads triaged within one working day | `/inbound`, `/organizations?lead_source=inbound&sort=inbound` |
| Daily | Open rights requests and their due dates; nothing overdue | dashboard → **Rights requests**, `/rights/requests` |
| Weekly | Tracked organisations re-checked (`recheck` jobs ran), CRM statuses pulled (`crm_pull_statuses`) | `/admin/jobs`, dashboard → **Tracking & CRM** |
| Monthly | HTTP Archive + CrUX feed run for last month (owner), review byte estimate in the run error if it failed closed | `/runs` → **HTTP Archive + CrUX feed** |
| Weekly | Retention report ran (`retention.run` in audit log) and backups exist (`docker compose exec backup ls /backups`) | `/admin/audit?action=retention.` |
| Every 90 days | Terms review per connector (Google Maps Platform, PSI, Team Cymru) — read the terms, then **Mark terms reviewed** | `/admin/connectors` |
| Quarterly | Rotate `API_TOKEN`; review users; confirm 2FA on owner accounts | `/admin/users` |

## 2. Jobs and the worker

- Queue: table `job`. Types: `places_text_search`, `website_probe`, `confirm_error_state`, `dns_asn`, `pagespeed`,
  `score_organization`, `retention_purge` and, since Phase 2, `crux_field_data`, `github_org_search`,
  `httparchive_feed_run`, `crm_sync`, `crm_pull_statuses`, `crm_delete`, `schedule_rechecks`, `link_known_contacts`.
  Idempotency keys prevent duplicates per day.
- Housekeeping the worker schedules itself (no cron needed): `schedule_rechecks` daily (weekly light re-probe per
  tracked organisation; PSI only every `RECHECK_PSI_AFTER_DAYS`), `crm_pull_statuses` every 6 hours,
  `retention_purge` daily. From a shell: `prospector recheck --wait`, `prospector crm pull`, `prospector retention`.
- Retry policy: 3 attempts, back-off 5 min × attempt. `CapExceeded` re-schedules for 00:05 UTC next day without
  consuming an attempt. `ConnectorDisabled` / `TermsReviewRequired` fail permanently (re-run after fixing).
- Retry a failed job: `/admin/jobs` → **Retry**. Drain once from a shell: `prospector worker --once`.
- Worker logs are redacted (e-mail addresses and phone-like strings replaced) — do not add raw PII to log lines.
- Finished jobs that carried transient Places data have their payload scrubbed (`"scrubbed": true`).

## 3. Rights requests (access / erasure / objection / correction / consent withdrawal) — target: 7 days, legal max per DPDP rules

1. `/rights/requests` → **Open a request** with the kind, the identifier the requester gave (e-mail, phone or domain)
   and the channel. The request gets a due date (`RIGHTS_DUE_DAYS`, default 30) and auto-matches organisations and
   contacts. Overdue requests are flagged on the dashboard.
2. Access request: on the request page, **export what we hold (JSON)** for each matched organisation and send it.
   Close the request with a resolution note.
3. Erasure / consent withdrawal: owner clicks **Erase everything & close**. In one transaction this deletes every
   matched contact, withdraws their consent records, scrubs their inbound submissions, removes the CRM contact
   (queued `crm_delete`), anonymises the matched organisations (and, if the box is ticked, the organisations the
   contacts belong to — untick it for a named employee of a company whose business facts should stay), adds the
   hashed identifiers to the suppression list and closes the request. The request keeps only a hash of the identifier.
4. Objection ("stop contacting me"): **Stop contact & close** suppresses the identifiers, marks contacts
   `objected` + do-not-contact and the organisation `suppressed`; records stay for the audit trail. For
   `withdraw_consent` it also deletes contacts whose only basis was consent.
5. Correction: edit the fact on the organisation page (manual facts carry source `manual`) and close with a note.
6. Everything is in the audit log automatically: `rights.request_open`, `rights.erasure` / `rights.objection`,
   `organization.anonymise`, `person.erase`, `suppression.add`, `crm.delete`.
7. Suppression hashes are kept indefinitely so a later discovery run, import or inbound form cannot re-create the record.

## 3a. Inbound forms (website webhook, hosted forms, Sign In with LinkedIn)

- Two public forms exist: `speed_evaluation` (SMB) and `agency_partner` (agency). They are served by Prospector at
  `${BASE_URL}/forms/<key>` (link to them from pb4host.com) **or** pb4host.com posts its own form to the webhook
  `POST ${BASE_URL}/inbound/forms/<key>` with an HMAC signature. Both paths require the consent checkbox; the consent
  text version, timestamp and hashed IP are stored as the proof.
- Webhook signature: headers `X-Prospector-Timestamp` (unix seconds) and `X-Prospector-Signature` =
  `sha256=` + HMAC-SHA256(`INBOUND_WEBHOOK_SECRET`, `"<timestamp>." + raw JSON body`). Replays older than
  `INBOUND_REPLAY_WINDOW_S` are rejected; identical payloads are idempotent. Minimal PHP handler for the website:

  ```php
  <?php // after validating the website form
  $payload = json_encode([
      'name' => $_POST['name'], 'email' => $_POST['email'], 'company' => $_POST['company'] ?? '',
      'website' => $_POST['website'] ?? '', 'phone' => $_POST['phone'] ?? '', 'message' => $_POST['message'] ?? '',
      'consent' => ['granted' => !empty($_POST['consent']), 'text_version' => '2026-10-v1'],
      'submitted_at' => gmdate('c'), 'source_url' => $_SERVER['HTTP_REFERER'] ?? '', 'client_ip' => $_SERVER['REMOTE_ADDR'],
  ], JSON_UNESCAPED_SLASHES);
  $ts = time();
  $sig = 'sha256=' . hash_hmac('sha256', $ts . '.' . $payload, getenv('PROSPECTOR_WEBHOOK_SECRET'));
  $ch = curl_init('https://prospector.example.com/inbound/forms/speed_evaluation');
  curl_setopt_array($ch, [CURLOPT_POST => true, CURLOPT_POSTFIELDS => $payload, CURLOPT_RETURNTRANSFER => true,
      CURLOPT_HTTPHEADER => ['Content-Type: application/json', "X-Prospector-Timestamp: $ts", "X-Prospector-Signature: $sig"]]);
  curl_exec($ch); // 201 = accepted (200 for an idempotent repeat); log and retry later on anything else
  ```

- What happens next (acceptance: enriched and visible in the CRM within 10 minutes): organisation resolved or created
  (`lead_source=inbound`), person created with `lawful_basis=consent` + a `consent_record`, website probe → DNS/ASN →
  PageSpeed → CrUX → score, then `crm_sync` pushes the account and contact to EspoCRM. Watch it on `/inbound` and the
  organisation page.
- Sign In with LinkedIn: register an app at developer.linkedin.com with product *Sign In with LinkedIn using OpenID
  Connect* and redirect URL `${BASE_URL}/auth/linkedin/callback`; set `LINKEDIN_CLIENT_ID/SECRET`. The button on the
  hosted forms only pre-fills and verifies name + e-mail; the access token is used once for `/v2/userinfo` and discarded.
  Nothing else is ever read from LinkedIn.
- Hosted-form abuse: honeypot field, per-IP rate limit (`INBOUND_RATE_LIMIT_PER_HOUR`), CSRF token; blocked attempts
  appear in the audit log as `inbound.rejected`.

## 3b. CRM (EspoCRM)

- Setup: run EspoCRM (bundled: `docker compose --profile crm up -d`, then `https://${CRM_HOST}`; first login `admin` /
  `ESPOCRM_ADMIN_PASSWORD`). Create an API user (Administration → API Users, auth method *API Key*, a role with
  Account + Contact create/read/edit/delete) and put the key in `ESPOCRM_API_KEY`; `ESPOCRM_BASE_URL=http://espocrm`
  inside compose. Restart `web` and `worker`; the organisation page shows a **CRM** card when configured.
- What is pushed: only organisations that are `qualified`/`exported`/`contacted`/`customer` or came in via a consented
  inbound form, never suppressed ones. Contacts are pushed only when their notice status is `sent` (or `not_required`
  for consented inbound), never when `objected`/do-not-contact, and never the owner's LinkedIn connections. The
  account description carries the score, signals, sources, evidence link and the exact notice text to use.
- When: automatically on inbound processing and on status changes to qualified/contacted/customer; manually with
  **Push now** or `prospector crm push <id>`. Re-pushes are skipped unless something changed (payload hash).
- Pull: every 6 hours the worker reads the Account `type` field (`CRM_STATUS_FIELD`) and maps it with `CRM_STATUS_MAP`
  (`Customer`→customer, `Prospect`→contacted, `Dead`→disqualified). The CRM never moves a customer backwards.
- Erasure propagates: `crm_delete` jobs remove the Account/Contact (EspoCRM keeps deleted rows in its recycle bin —
  empty it from Administration → Clear Cache / the `deleted` flag job — or purge directly for a formal erasure).

## 3c. People you know (owner's LinkedIn connections)

- Owner only: Imports → **LinkedIn connections** with the `Connections.csv` from LinkedIn *Settings → Data privacy →
  Get a copy of your data*, or `prospector import-connections Connections.csv`. Nothing is fetched from LinkedIn.
- Rows become people with `lawful_basis=legitimate_interest`, the LIA reference from
  `LINKEDIN_CONNECTIONS_LIA_REFERENCE`, notice `pending`, linked to an organisation when the company name matches
  (automatic re-linking runs after every import and discovery run). `/people` lists them; the organisation list and
  detail pages show a **known** badge; the review queue has a *people I know* filter.
- These rows are for personal one-to-one messages only: they are excluded from bulk exports and only reach the CRM
  once a notice has been recorded. Re-importing updates rows instead of duplicating them.

## 3d. Discovery feeds (Phase 2)

- GitHub agencies (`/runs` → **GitHub agencies**, `prospector github-run "Bengaluru"`): organisations with PHP
  repositories in a location; stores name, website, location, repo counts — never members or e-mails. GitHub data
  must not be used for unsolicited e-mail (GitHub AUP §7): the resulting agency leads are for 1:1 outreach via the
  agency's own contact channels.
- HTTP Archive + CrUX monthly feed (owner only; `prospector feed httparchive 2026-09`): one BigQuery query against the
  public `httparchive.crawl.pages` + `chrome-ux-report.materialized.country_summary` datasets returning WordPress origins
  with poor real-user mobile LCP in `HTTPARCHIVE_DEFAULT_COUNTRY`. Needs `pip install ".[bigquery]"` (or the
  `bigquery` extra in the image), `BIGQUERY_PROJECT` and a service-account file. The connector dry-runs first and
  **fails closed** above `HTTPARCHIVE_MAX_BYTES` (default 100 GB ≈ within the 1 TB/month free tier for ~10 runs).
- CrUX API: field data per origin after each probe (needs `CRUX_API_KEY` or reuses `PSI_API_KEY`); scoring merges lab
  (Lighthouse) and field (CrUX) numbers — `field_poor` fires on LCP p75 > 4 s or INP p75 > 500 ms.
- Companies House (UK): on a GB organisation page search the register, **Apply** stores legal name, company number and
  registered address as facts with source `companies_house`; **view officers** is a live, audited view that is never
  stored. Key from developer.company-information.service.gov.uk, 600 requests / 5 minutes.

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
- CrUX API: free, 150 queries/minute; the app caps at 1,000/day. GitHub REST: 5,000 requests/hour with a token, search
  30/minute; the app caps at 300/day and backs off on `X-RateLimit-Remaining: 0`. Companies House: free, 600 per
  5 minutes; cap 500/day. BigQuery: first 1 TB of query bytes per month free — the feed refuses anything above
  `HTTPARCHIVE_MAX_BYTES` before running. EspoCRM: self-hosted, no per-call cost; cap 2,000/day as a safety net.

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
  Alembic. PostgreSQL uses `prospector migrate` (Alembic `0001_initial_schema`, `0002_phase2_inbound_rights_crm`).
  Upgrading a Phase 1 database: `prospector migrate` (SQLite dev databases too — the migration uses batch mode).
- `prospector demo-seed` creates clearly synthetic organisations (`Demo:` prefix, `.example` domains, source
  `demo_seed`) so the UI can be explored without any external call. Delete them with
  `DELETE FROM organization WHERE domain LIKE '%.example'` or just re-create the dev database.
- Tests never touch the network: `pytest -q`.
