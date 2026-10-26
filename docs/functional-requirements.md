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
| FR-C11 | Environment and business service can be corrected locally with a mandatory reason, without changing the source value, interrupting the AssetExplorer sync, or re-matching the asset. The effective value is used by lists, the engine, reports and Grafana; the override is attributed and audited | `inventory.asset_overrides`, `inventory.asset_effective`; `PUT`/`DELETE /assets/{id}/overrides`; `inventory:manage` |
| FR-C12 | The environment and business-service values offered in the placement editor are configured in Settings and can be added, renamed and deleted there, so operators choose from a list instead of retyping a value. A rename propagates to the inventory, filters, dashboard, compliance engine, reports and Grafana by resolving the old spelling through an alias, without rewriting the source value or any stored override, and survives the next sync. A value still in use cannot be deleted. Usage counts precede a change and every change is audited | `inventory.placement_options`, `inventory.placement_aliases`; `GET/POST/PATCH/DELETE /settings/placement`; `settings:manage`; `inventory.asset_effective` (alias-aware) used for display and usage counts |

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
| FR-E2 | Credentials are referenced, never stored | `secret_ref` accepts `env:` / `file:` only; `secrets.validate_reference` |
| FR-E3 | A pasted credential is rejected with an explanatory error | `inventory /integrations/{key}` |
| FR-E4 | TLS verification is on by default and cannot be silently disabled | `verify_tls` default true; plain HTTP without explicit opt-out is 422; disabling is audited |
| FR-E5 | Each run is recorded with counts and an error summary | `inventory.sync_runs` |
| FR-E6 | Manual sync can be triggered by an authorised user | `POST /sync` (`sync:run`) |
| FR-E7 | Integration health is exposed with the last run and last success | `inventory /integrations`, `_health()` |

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
| FR-H13 | Expired report files are purged after the retention period; metadata remains | `purge_expired` |
| FR-H14 | The PDF states the methodology, the freshness threshold and the blocking severities | `render_report` methodology section |

## Dashboard and API (FR-I)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-I1 | Executive dashboard with KPIs, breakdowns, donut, trend, quarterly target, severity | `gateway/static/*`, `/api/compliance/summary` |
| FR-I8 | The trend is shown on a monthly (12 points), quarterly (8) or yearly (5) basis, each point the average of the daily snapshots in that calendar period with its sample count | `/api/compliance/trends?period=…`, `gateway/static/app.js` |
| FR-I9 | The dashboard refreshes itself on an operator-selected interval (30 s, 1 min, 5 min) and the navigation can be hidden for a wall display | `startDashboardRefresh`, `setKiosk` in `gateway/static/app.js` |
| FR-I10 | Every list-styled register supports free-text search across all columns of a row, per-field filtering and sorting with a direction toggle; the shown/total row count is visible when a filter is active. A compliance-state name matches as a whole term, so `Compliant` never also returns `Non-compliant` rows | `listPanel` in `gateway/static/app.js`; multi-column `search` plus `sort`/`direction` on `/compliance/servers` and `/inventory/assets`; exact `c.state = :state_search` and the inventory `compliance_state` Status filter |
| FR-I11 | The operating-system breakdown groups by platform family (RHEL, Microsoft Windows, Solaris, Other), not version; the environment and business-service breakdowns use the effective (override-aware) values; every group in the result is shown, with a scrolling list when there are many | `/compliance/summary` `by_os` / `by_environment` / `by_business_group`; `group_breakdown`; `statusChartCard` / `stackedRows` in `gateway/static/*` |
| FR-I2 | Filters by OS, environment, business group, application group, patch source | Filter parameters on `/summary` and `/servers` |
| FR-I3 | A Grafana dashboard is provisioned from version-controlled JSON | `deploy/grafana/` |
| FR-I4 | Grafana uses the same definitions as the API | SQL mirrors `engine` predicates; documented in [reporting.md](reporting.md) |
| FR-I5 | The application works with Grafana stopped | Verified; Grafana is a separate optional profile |
| FR-I6 | Drill-down from any figure to the servers behind it | `/compliance/servers`, `/compliance/findings` |
| FR-I7 | Health endpoints for every service | `/healthz` (liveness), `/readyz` (database readiness) |

## Audit (FR-J)

| ID | Requirement | Implementation |
|----|-------------|----------------|
| FR-J1 | Audit events are append-only at the database level | Trigger rejecting `UPDATE`/`DELETE` |
| FR-J2 | Secrets are never written to the audit context | `audit.scrub` with a sensitive-key denylist |
| FR-J3 | Audit is queryable by actors, type, outcome and time | `identity /audit` |
| FR-J4 | Audit records carry actor, role, target, outcome, source IP and service | `shared.audit_events` columns |

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
  The connector implements the documented `/api/v3/assets/servers` shape with a
  configurable field map; it must be verified against the customer's
  AssetExplorer version before being enabled.
- **SCCM collection requires an approved read-only view or export.** The
  connector targets the Administration Service REST shape; a customer whose
  SCCM has no such endpoint must supply the export path.
- **Solaris collection is a file drop.** No SSH collection exists, by design.
- **OIDC sign-in is not implemented**, though the schema and the "external
  identity" error path exist.
- **No Docker-based end-to-end test has been run on this host** (Docker is not
  installed here); the compose topology is verified by inspection and the
  native launcher is verified by execution. See
  [testing-strategy.md](testing-strategy.md).
