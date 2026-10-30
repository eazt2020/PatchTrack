# Functional requirements

Each requirement carries a stable identifier used in
[traceability-matrix.md](traceability-matrix.md). "Implemented" means the code
exists and is exercised by the test suite; "configured" means it depends on a
real source system this repository cannot reach.

## Authentication and session (FR-A)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-A1 | Sign-in with username and password; passwords hashed with Argon2id | `identity/app/main.py::login`; `PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)` |
| FR-A2 | Minimum 12-character password, enforced server-side | `UserCreate.password` / `PasswordReset.password` (`min_length=12`) |
| FR-A3 | Account locks for 15 minutes after 5 consecutive failures | `MAX_FAILED_ATTEMPTS`, `LOCKOUT_MINUTES`, `identity.users.locked_until` |
| FR-A4 | Lockout does not reveal whether the account exists | Same 401 body for unknown user and wrong password; `login_attempts` is keyed by submitted username |
| FR-A5 | Successful sign-in issues a 30-minute access token and an 8-hour refresh token | `access_token_minutes=30`, `refresh_token_hours=8` |
| FR-A6 | Refresh tokens are stored only as SHA-256 digests and are revocable | `identity.refresh_tokens.token_hash`; revoked on logout |
| FR-A7 | The browser session is an HttpOnly, SameSite=Strict cookie | `gateway/app/main.py::_establish_session` |
| FR-A8 | First-run administrator creation, available only while zero users exist | `identity/bootstrap/status`, `gateway /api/session/bootstrap`; no shipped password |
| FR-A9 | Every sign-in attempt, success or failure, is audited | `auth.login` events with outcome |
| FR-A10 | There is no universal default account | Migration 002 inserts no rows; bootstrap refuses once a user exists |

## Users and roles (FR-B)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-B1 | Four roles: viewer, operations, report_manager, administrator | `security.PERMISSIONS` |
| FR-B2 | Create, update, deactivate users; reset passwords; change role | `identity /users` routes |
| FR-B3 | Administrators cannot silently demote the last administrator | Enforced in `update_user` (see code) |
| FR-B4 | Role changes are audited with old and new role | `user.updated` audit event |
| FR-B5 | Every backend route declares the permission it needs | `Depends(require_permission(...))` on all non-health routes |

## Inventory (FR-C)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-C1 | Assets synchronised from AssetExplorer with a configurable field map | `connectors/assetexplorer.py::DEFAULT_FIELD_MAP`, `extra.field_map` override |
| FR-C2 | `assetexplorer_id` is immutable and unique | `inventory.assets.assetexplorer_id UNIQUE`; never updated by `upsert_asset` |
| FR-C3 | An asset missing from a response is not deleted | `run_assetexplorer_sync` only upserts |
| FR-C4 | Assets are marked retired only when the source says so | `LIFECYCLE_MAP` |
| FR-C5 | Historical hostnames, FQDNs and IPs are remembered as aliases | `inventory.asset_aliases` |
| FR-C6 | A re-sync with unchanged data records no change | `host(ip_address)` normalisation; verified by integration test |
| FR-C7 | Search and filter by hostname, FQDN, IP, asset id, OS, environment, business group, application group, patch source, lifecycle, compliance state, scope | `inventory /assets` |
| FR-C8 | Paginated listing with a stable sort | `page`, `page_size`, `sort`, `direction` |
| FR-C9 | Assets can be excluded and re-included, with a reason and an actor | `/assets/{id}/exclude`, `/include`; audited |
| FR-C10 | Dimension values are exposed for filter dropdowns | `inventory /dimensions` |
| FR-C11 | Environment and business service can be corrected locally with a mandatory reason, without changing the source value, interrupting the AssetExplorer sync, or re-matching the asset. The effective value is used by lists, the engine, reports; the override is attributed and audited | `inventory.asset_overrides`, `inventory.asset_effective`; `PUT`/`DELETE /assets/{id}/overrides`; `inventory:manage` |
| FR-C12 | The environment and business-service values offered in the placement editor are configured in Settings and can be added, renamed and deleted there, so operators choose from a list instead of retyping a value. A rename propagates to the inventory, filters, dashboard, compliance engine and reports by resolving the old spelling through an alias, without rewriting the source value or any stored override, and survives the next sync. A value still in use cannot be deleted. Usage counts precede a change and every change is audited | `inventory.placement_options`, `inventory.placement_aliases`; `GET/POST/PATCH/DELETE /settings/placement`; `settings:manage`; `inventory.asset_effective` (alias-aware) used for display and usage counts |

## Reconciliation (FR-D)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-D1 | A patch record is matched by source link first, then FQDN, hostname, IP | `sync.match_asset` |
| FR-D2 | An ambiguous match is never guessed | Returns `(None, "ambiguous")`, recorded in `reconciliation_conflicts` |
| FR-D3 | Unmatched patch records are counted and surfaced, not dropped silently | `sync_runs.assets_unmatched`, reconciliation view |
| FR-D4 | A direct link, once established, is stable across collections | `inventory.asset_source_links` unique on (source_system, source_ref) |
| FR-D5 | An identifier moving between assets raises a conflict | `_maybe_conflict`, `identifier_changed` |
| FR-D6 | A patch assessment is never associated with a different asset because a hostname was reused | Alias table retains history; conflict blocks the association |
| FR-D7 | Conflicts can be resolved by an administrator with a recorded resolution | `POST /reconciliation/{id}/resolve` |
| FR-D8 | A failed source collection leaves inventory and previous assessments intact | `run_*_sync` writes assessments only after a successful fetch |

## Integrations (FR-E)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-E1 | Four integrations, seeded disabled with no endpoint and no credential | Migration 006 |
| FR-E2 | Integration credentials are referenced, never stored (`env:` / `file:` only). The one database-stored credential is the Settings SMTP password, encrypted at rest and write-only (FR-H17) | `secret_ref` accepts `env:` / `file:` only; `secrets.validate_reference` |
| FR-E3 | A pasted credential is rejected with an explanatory error | `inventory /integrations/{key}` |
| FR-E4 | TLS verification is on by default and cannot be silently disabled | `verify_tls` default true; plain HTTP without explicit opt-out is 422; disabling is audited |
| FR-E5 | Each run is recorded with counts and an error summary | `inventory.sync_runs` |
| FR-E6 | Manual sync can be triggered by an authorised user | `POST /sync` (`sync:run`) |
| FR-E7 | Integration health is exposed with the last run and last success | `inventory /integrations`, `_health()` |
| FR-E8 | An administrator can test an integration's configuration without collecting: endpoint, TLS and credential are exercised against the source and the result states success or the failure reason, with the duration. A draft from the editor can be tested before saving and is not persisted. A test writes no asset, assessment or sync run; it is audited as `integration.tested` with the outcome and reason | `POST /integrations/{key}/test`, `_probe_integration` in `inventory/app/main.py`; connector `probe()` methods; Integrations screen in `gateway/static/app.js` |

## Compliance evaluation (FR-F)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-F1 | Five states; exactly one per asset | `engine.evaluate_asset` |
| FR-F2 | Population = `in_scope` and not retired/decommissioned | Engine rules 1 |
| FR-F3 | Missing assessment ⇒ Unknown, in the denominator | Engine rule 2 |
| FR-F4 | Assessment older than `freshness_hours` ⇒ Stale, in the denominator | Engine rule 3; compared on exact, unrounded age |
| FR-F5 | Failed assessment ⇒ Unknown | Engine |
| FR-F6 | Otherwise Compliant or Non-compliant by blocking severities | `rule_for` + `blocking_severities` |
| FR-F7 | Compliance percentage is `compliant / (compliant + non_compliant + unknown + stale)` | `Population.compliance_pct` |
| FR-F8 | An empty denominator yields `null`, never `0` or `100` | `Population.compliance_pct` |
| FR-F9 | Server-level percentage and patch-level counts are never blended | Separate fields end to end |
| FR-F10 | Policy is versioned; each version is immutable | `compliance.policy_versions`; each snapshot records `policy_version_id` |
| FR-F11 | Scope rules can vary blocking severities and remediation days by OS/environment/business/application group | `policy.scope_rules`, first match wins |
| FR-F12 | Remediation due dates derive from severity and release date | `engine.remediation_due` |
| FR-F13 | Period resolution accepts `YYYY-MM` and `YYYY-Qn` and is identical in both services | `period_bounds` parity test |
| FR-F14 | Unknown severity is never treated as non-blocking by omission | Findings with unmapped severity are recorded `unknown` and counted |

## Snapshots and history (FR-G)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-G1 | A daily snapshot is captured automatically and is idempotent per day | `ensure_daily_snapshot`, partial unique index |
| FR-G2 | Snapshots store totals, per-dimension groups, policy version and methodology | `create_snapshot` |
| FR-G3 | Snapshots are never updated or deleted | No update path exists; retention applies to report *files*, not snapshots |
| FR-G4 | Trend is derived from snapshots rolled up by month | `compliance /trends` |
| FR-G5 | Quarterly snapshots are retained for the compliance history | `scope='quarterly'` |

## Reporting (FR-H)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-H1 | Report types: monthly management, quarterly compliance, inventory register | `REPORT_TITLES`, `reporting.reports.report_type` |
| FR-H2 | A report is generated only from an existing snapshot for that period | `409` when no snapshot exists |
| FR-H3 | The PDF is validated before publication | `validate_pdf` (magic bytes, size, EOF) |
| FR-H4 | The file is written atomically and its SHA-256 recorded | `.pdf.tmp` + `Path.replace`; `sha256` column |
| FR-H5 | The archived report is downloadable and identified by digest | `GET /reports/{id}/download` |
| FR-H6 | A failed generation is recorded with the error, not left `queued` | `status='failed'`, `error_summary` |
| FR-H7 | A failed report can be retried without altering a completed one | `POST /reports/{id}/retry` |
| FR-H8 | Schedules: monthly or quarterly, day, hour, timezone, recipients | `reporting.schedules` |
| FR-H9 | A scheduled period runs at most once | `job_executions UNIQUE (schedule_id, period_start)` |
| FR-H10 | Month-end clamping keeps a "31st" schedule running every month | `calendar.monthrange` in `compute_next_run` |
| FR-H11 | Delivery status is recorded separately from generation status | `delivery_attempts`, `sent`/`failed`/`skipped` |
| FR-H12 | A failed email never changes a completed report's status | `deliver_report` writes only a delivery attempt |
| FR-H13 | Report file retention is configurable at runtime by an administrator (30–36,500 days, default 2,555) and applied immediately: saving recomputes every completed report's expiry from its completion date and purges already-expired files in the same request; new reports record their expiry at completion; the daily job continues to enforce the window. Only the file is deleted — the report row, digest and size remain — and each change is audited | `GET`/`PUT /settings/report-retention` in `reporting/app/main.py`; `report_retention_days`, `purge_expired` in `reporting/app/service.py`; Settings → Retention → Report file retention in `gateway/static/app.js` |
| FR-H14 | The PDF states the methodology, the freshness threshold and the blocking severities | `render_report` methodology section |
| FR-H15 | The PDF opens with a cover carrying the configured organisation logo and name, report title, period and classification, and ends with a sign-off page containing separate **Prepared by** and **Concurred by** blocks. Branding (logo, organisation name, preparer and concurrence name/designation) is configured in Settings, stored in `shared.app_settings`, and a logo upload is limited to 1 MB and verified as PNG or JPEG before it can be embedded. A name-only save preserves an existing logo; removal is explicit | `_cover_story`, `_signoff_story`, `_logo_flowable` in `reporting/app/pdf.py`; `GET/PUT /settings/organization`; `inventory.app.main._validated_logo` |
| FR-H16 | Every background job (four collections, daily snapshot, audit purge, report tick, report retention) is listed with its interval, enabled state, next run and last result on **Reporting → Schedules**. An administrator can change an interval (minimum 10 seconds) or disable a job; the change is stored in `shared.scheduled_jobs`, takes effect on the next poll without a restart, is recorded as `scheduler.updated`, and survives a restart. A new interval is counted from the save; re-enabling schedules the next run one interval out | `GET/PATCH /scheduler/jobs` in `reporting/app/main.py`; `due_jobs`, `_configuration` in `patchtrack_common/scheduler.py`; migration `019_scheduler_ui.sql` |
| FR-H17 | The SMTP relay used for emailed reports is configured in Settings (host, port, STARTTLS, username, password, sender), stored in `shared.app_settings`, applies without a restart, and overrides the environment fallback. The password is write-only and encrypted at rest; no response or audit event contains it. A test route sends a message without saving. When the saved password cannot be decrypted, delivery fails with an instruction to re-enter it rather than sending unauthenticated | `smtp_config.py`, `mailer.py` in `reporting/app/`; `GET/PUT /settings/smtp`, `POST /settings/smtp/test`; Settings → Email delivery in `gateway/static/app.js` |
| FR-H18 | A new installation serves HTTPS without manual TLS setup: the gateway generates a self-signed certificate on first start under `PATCHTRACK_TLS_DIR` and refuses to start with only half a pair. An administrator can install the organisation's PEM certificate and unencrypted key in Settings; the pair is validated (parses, currently valid, key matches), replaces the previous files atomically with the key mode 0600, applies to new connections without a restart, and is audited with subject, issuer, expiry and SHA-256 but never the key. TLS 1.2 is the floor and HSTS is emitted over HTTPS. An external proxy remains supported with `PATCHTRACK_TLS_ENABLED=false` | `patchtrack_common/tls.py`, `patchtrack_common/serve.py`; `GET/PUT /api/tls`; Settings → HTTPS certificate in `gateway/static/app.js` |

## Dashboard and API (FR-I)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-I1 | Executive dashboard with KPIs, breakdowns, donut, trend and quarterly target; the patch-count severity distribution is shown on Remediation, keeping patch counts apart from the server percentage | `gateway/static/*`, `/api/compliance/summary`; severity bars on `/findings` |
| FR-I8 | The trend is shown on a monthly (12 points), quarterly (8) or yearly (5) basis, each point the average of the daily snapshots in that calendar period with its sample count. A single Timeframe control selects both the live/historical view and the trend basis: Live offers one option per basis, and a recorded period implies its own kind (month → monthly, quarter → quarterly, year → yearly) | `/api/compliance/trends?period=…`, `gateway/static/app.js` |
| FR-I9 | The dashboard refreshes itself on an operator-selected interval (30 s, 1 min, 5 min) and the navigation can be hidden for a wall display | `startDashboardRefresh`, `setKiosk` in `gateway/static/app.js` |
| FR-I10 | Every list-styled register supports free-text search across all columns of a row, per-field filtering and sorting with a direction toggle; the shown/total row count is visible when a filter is active. A compliance-state name matches as a whole term, so `Compliant` never also returns `Non-compliant` rows | `listPanel` in `gateway/static/app.js`; multi-column `search` plus `sort`/`direction` on `/compliance/servers` and `/inventory/assets`; exact `c.state = :state_search` and the inventory `compliance_state` Status filter |
| FR-I11 | The operating-system breakdown groups by platform family (RHEL, Microsoft Windows, Solaris, Other), not version; the environment and business-service breakdowns use the effective (override-aware) values; every group in the result is shown, with a scrolling list when there are many | `/compliance/summary` `by_os` / `by_environment` / `by_business_group`; `group_breakdown`; `statusChartCard` / `stackedRows` in `gateway/static/*` |
| FR-I12 | The dashboard can show a past month, quarter or year, selectable from the periods that have a recorded snapshot. A historical view is built from the immutable daily snapshot captured in that period (the latest one), never from a re-evaluation of the current estate, and names the snapshot and the number of daily snapshots in the period. An empty period is refused rather than rendered blank, and a dimension filter cannot be combined with a historical period because a snapshot stores group totals, not server rows | `GET /compliance/summary?period=YYYY-MM\|YYYY-QN\|YYYY`, `GET /compliance/snapshot-periods`; `parse_period_key`, `_period_summary`; Timeframe control in `gateway/static/app.js` |
| FR-I13 | Register list tables offer a CSV export of the list as currently filtered and sorted. Client-filtered registers export the visible subset; server-paged registers fetch every matching page before writing the file. Action-only columns are omitted, values match what the table displays, and the download is UTF-8 with a BOM for spreadsheet compatibility. The Executive dashboard, Operational health, Settings and Reports are deliberately excluded | `table()` in `gateway/static/ui.js`; `allPages()` and `exportRows` in `gateway/static/app.js` |
| FR-I14 | An operator can record a verification of a non-compliant, stale or unknown server: a reason from a fixed list, a substantive statement, a remediation plan and target date, and up to five evidence files (PDF, image, text, CSV or Office, 5 MB each). Posting a new record supersedes the previous one without deleting it; withdrawal keeps the history. The compliance state is never altered by a justification. Every record, evidence upload and withdrawal is audited, and evidence is downloadable only through an authenticated, permission-checked route | `inventory/app/justifications.py`, `inventory/asset_justifications`, `inventory/justification_evidence` (migration 015); `/inventory/assets/{id}/justifications`; verification card in `gateway/static/app.js` |
| FR-I2 | Filters by OS, environment, business group, application group, patch source | Filter parameters on `/summary` and `/servers` |
| FR-I6 | Drill-down from any figure to the servers behind it | `/compliance/servers`, `/compliance/findings` |
| FR-I7 | Health endpoints for every service | `/healthz` (liveness), `/readyz` (database readiness) |

## Audit (FR-J)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-J1 | Audit events are append-only at the database level; `UPDATE` is always refused, and `DELETE` is refused except through the retention purge function | `shared.audit_events_immutable()` trigger; `shared.purge_audit_events()` (migration 012) |
| FR-J2 | Secrets are never written to the audit context | `audit.scrub` with a sensitive-key denylist |
| FR-J3 | Audit is queryable by actors, type, outcome and time | `identity /audit` |
| FR-J4 | Audit records carry actor, role, target, outcome, source IP and service | `shared.audit_events` columns |
| FR-J5 | Audit retention is configurable at runtime by an administrator (30–36,500 days, default 2,555). The window is stored in the database and applied by a daily job; only events older than the cutoff are deleted, the purge is itself audited, and no API can edit or delete an individual event | `GET`/`PUT /settings/audit-retention`, `POST /jobs/audit-purge` in `identity/app/main.py`; `shared.purge_audit_events` in migration 012; Settings → Retention → Audit trail retention in `gateway/static/app.js` |

## Operations (FR-K)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-K1 | Deterministic migrations with checksum verification | `patchtrack_common.migrate` |
| FR-K2 | A modified applied migration is an error, not a silent re-run | checksum comparison raises |
| FR-K3 | All services run as non-root with a read-only root filesystem | `deploy/container/Dockerfile`, `docker-compose.yml` |
| FR-K4 | Health checks gate startup ordering | compose `healthcheck` + `service_healthy` |
| FR-K5 | Installation works offline from a prepared bundle | [offline-deployment.md](offline-deployment.md) |

## Known gaps for this release

- **AssetExplorer API shape is not confirmed against a live installation.**
  The connector implements ManageEngine's documented V3 Assets API
  (`/api/v3/asset_servers`, `input_data` paging, `AUTHORIZATION` integration
  key, build 7700+) with a configurable field map. It has been exercised
  against a simulator of that documented shape; custom field names and the
  customer's build still need verification before it is enabled.
- **SCCM collection requires an approved read-only compliance facade.** The
  native Administration Service exposes device identity but no joined
  missing-updates projection, and requires Windows-integrated authentication;
  the connector sends a bearer token. A customer must supply a facade (or
  export) exposing one device row with `MissingUpdates`; `extra.api_path`
  selects its route.
- **Solaris is collected directly over SSH, per host.** Solaris 11.4+ uses the
  host's IPS catalogue to report a pending SRU; Solaris 10 can only enumerate
  installed patches and is assessed against an administrator-supplied baseline
  (no baseline means Unknown, not compliant). The collector has been exercised
  against unit-level fixtures and a documented contract, not against a live
  Solaris server; the first deployment should test each target before enabling
  its schedule. The watched-inbox path remains for hosts SSH cannot reach.
- **OIDC sign-in is not implemented**, though the schema and the "external
  identity" error path exist.
- **No Docker-based end-to-end test has been run on this host** (Docker is not
  installed here); the compose topology is verified by inspection and the
  native launcher is verified by execution. See
  [testing-strategy.md](testing-strategy.md).
