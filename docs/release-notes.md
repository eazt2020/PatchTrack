# Release notes

## 3.0.0 — 2026-10-26

First release.

### Capabilities

- **Inventory.** ManagedEngine AssetExplorer connector with a configurable
  field map, immutable `assetexplorer_id`, alias history for renames, and
  reconciliation that refuses to guess when a match is ambiguous.
- **Patch collection.** Red Hat Satellite (host errata), Microsoft
  Configuration Manager (missing updates), and Solaris via an approved
  read-only file drop. No SSH collection anywhere.
- **Local placement overrides.** Operations and administrators can correct a
  server's environment or business service when AssetExplorer is late or wrong.
  The correction is stored beside the source value (`inventory.asset_overrides`),
  the sync keeps running and keeps updating its own columns, and every reader —
  lists, the compliance engine, reports and Grafana — uses the effective value
  through `inventory.asset_effective`. A reason is mandatory, the change is
  audited, the row is marked `LOCAL` with the source value in its tooltip, and
  clearing it returns the AssetExplorer value without a re-sync.
- **Curated placement lists.** Administrators maintain the environment and
  business-service values an operator can choose in **Settings**
  (`inventory.placement_options`): add, rename and delete, with duplicate
  values rejected case-insensitively and the current usage count shown before
  a change. The editor offers the list instead of free text, so a correction
  cannot introduce a second spelling of an existing value. A **rename
  propagates**: the old spelling is recorded in
  `inventory.placement_aliases` and resolved on read by
  `inventory.asset_effective`, so the inventory, filters, dashboard panels,
  compliance engine, reports and Grafana all show the new name while the source
  value is preserved as `*_source` and the next sync cannot restore it. A value
  still in use cannot be deleted — rename it. The AssetExplorer sync never
  writes either table.
- **Integration self-test.** Each integration card and the configuration editor
  can test the source without collecting: endpoint reachability, TLS, credential
  and the first page's shape are checked and reported with a reason and
  duration. A draft can be tested before saving. The probe writes no asset,
  assessment or sync run, so a failed test cannot disturb compliance or the
  integration's health; it is audited as `integration.tested`. For Solaris the
  test verifies the inbox exists, is readable and every waiting document parses.
- **Compliance engine.** Five states (Compliant, Non-compliant, Unknown,
  Stale, Excluded) with a documented numerator and denominator. Unknown and
  stale are counted against compliance. Server percentage and patch counts are
  never blended. Policy is versioned, and every snapshot records the version in
  force.
- **Immutable history.** Daily snapshots with a per-day unique constraint;
  monthly, quarterly and on-demand snapshots; a trend derived from snapshots.
- **Reporting.** Monthly management, quarterly compliance and inventory register
  PDFs, with an executive summary, breakdowns, remediation overview, trend,
  data-quality statement and methodology. Validated before publication,
  archived with a SHA-256. Monthly and quarterly schedules with idempotent
  execution. Email delivery tracked separately; a failed send never invalidates
  a report.
- **Dashboard.** A vanilla-ES-module SPA served by the gateway, plus a
  provisioned Grafana dashboard reading the same tables through a read-only
  role. The application works with Grafana stopped. The executive view offers a
  **Period** control that switches the tiles and breakdowns between the live
  estate and any past month, quarter or year with a recorded snapshot. A
  historical view is built from the latest immutable daily snapshot captured
  inside that period, names the snapshot and the number of daily snapshots it
  rests on, never re-evaluates today's estate under a past label, and refuses a
  dimension filter (a snapshot stores group totals, not server rows). Empty
  periods are not offered. The same view offers a monthly (12 points),
  quarterly (8) or yearly (5) trend basis derived from the
  immutable daily snapshots — each point is the average of the snapshots in
  that calendar period, with the sample count shown — an optional
  auto-refresh (30 s, 1 min, 5 min; pauses while the tab is hidden or a dialog
  is open), and a TV/wall mode that hides the navigation for NOC/SOC displays
  (button, `K`, `Esc`, and the `#/dashboard?tv=1` deep link).
- **Filterable, sortable registers.** Every list — non-compliant servers,
  remediation, the snapshot ledger, server inventory, reconciliation, report
  history, schedules, integrations, compliance policy, users and the audit
  trail — carries a search box, per-field filters and a sort field with a
  direction toggle. Search spans **every column of the row**, flattened,
  rather than a nominated field: one term can match a hostname, an operating
  system, an environment, a business service, a status, an owner, a location or
  a note. Registers held in the page narrow and re-order locally and report
  "showing X of Y rows"; the two paged registers (server list, inventory) pass
  the term and a whitelisted sort key to the API, whose SQL matches the same
  set of columns, so the whole result set is filtered and ordered, not just the
  25 rows on screen.
- **Audit retention.** The retention window is set in **Settings → Audit trail
  retention** (30–36,500 days, default 2,555) and stored in
  `shared.app_settings`, so a change needs no restart. A daily job deletes
  events older than the cutoff through `shared.purge_audit_events()`; the
  function enforces the range itself and takes no cutoff argument. Everything
  else about the trail remains append-only, and both the setting change and
  each purge are audited.
- **Access control.** Four roles enforced by the backend on every route;
  Argon2id passwords; account lockout; HttpOnly session cookie; CSRF
  double-submit; strict CSP; append-only audit trail with configurable
  retention.

### Deployment

- Docker/Podman Compose on RHEL 9.8 and Rocky 9.8 with SELinux enforcing.
- Native systemd units in `deploy/systemd/` for hosts where containers are
  prohibited: a oneshot migration unit that every service requires, and a
  templated per-service unit with no built-in port so five instances cannot
  collide. The hourly timer is documented in the runbook rather than shipped.
  Validated with `systemd-analyze verify` and pinned by unit tests; never
  started here.
- Single UBI9 python-312 image for all five services, running as UID 1001,
  read-only root filesystem, all capabilities dropped.
- Offline installation from a verified image and wheel bundle.
- No credential, key or default password ships with the product. The first
  administrator is created through a one-time form.

### Database migrations

| Version | Change |
|---------|--------|
| `000_extensions` | `citext`, `pgcrypto` |
| `001_bootstrap_shared` | Schemas, migrations ledger, audit table + immutability trigger, settings |
| `002_identity` | Users, refresh tokens, login attempts |
| `003_inventory` | Integrations, sync runs, assets, aliases, source links, conflicts |
| `004_compliance` | Policies, versions, assessments, findings, states, snapshots, groups, Solaris uploads |
| `005_reporting` | Reports, schedules, delivery attempts, job executions |
| `006_integration_catalogue` | Four integrations seeded disabled, no endpoint, no credential |
| `007_snapshot_dimensions` | Aligned the snapshot group dimension constraint with what the evaluator emits |
| `008_asset_overrides` | Local environment / business-service overrides and the `inventory.asset_effective` view |
| `009_placement_options` | Environment / business-service picker values, seeded from the values already present in the inventory |
| `010_placement_aliases` | Previous names of a placement value, resolved on read by `inventory.asset_effective`, so a Settings rename reaches every reader and survives the next sync |
| `011_placement_backfill` | Adds any in-use value an earlier rename left out of the list; invents nothing |
| `012_audit_retention` | Configurable audit retention: the purge function, a retention-only exception in the immutability trigger, and an enforced 30–36,500 day range |

Fresh installation: all thirteen apply in order.

### Fixes found and corrected during development

These were real defects caught by the test suite before release; they are
recorded because each one changed a published figure or a stored record.

- **Freshness rounding slack.** Assessment age was rounded to two decimals
  before being compared with the threshold, so an assessment up to 30 seconds
  over the limit still counted as Compliant. Now compared on the exact
  duration.
- **Phantom IP-change conflicts.** `inet::text` appends `/32`, so every
  unchanged re-sync appeared as an IP change and raised a spurious
  `identifier_changed` conflict. Now compared with `host(ip_address)`.
- **Month-end scheduler drift.** A schedule on the 26th advanced to the 28th
  after its run time passed, and a 31st schedule could not run in February.
  Now clamped to the last day of the target month.
- **Grafana `$__conditionalAll`.** The macro produced invalid SQL for these
  queries. Replaced with an explicit `__all__` sentinel and a plain
  `('$var' = '__all__' OR col = '$var')` predicate.
- **Snapshot dimension constraint.** The original check rejected the dimensions
  the evaluator actually emits, so any snapshot capture failed. Corrected in
  migration 007.
- **Gateway header rendering.** The freshness indicator rendered as
  `[object HTMLSpanElement]` because a DOM node was assigned to `textContent`.
- **A Settings rename did not reach the inventory.** Renaming a placement value
  changed the picker while every server kept the old spelling, and a value
  still in use could be deleted, so the inventory, panels, engine, reports and
  Grafana could disagree with Settings. Renames now propagate through
  `inventory.placement_aliases` resolved at read time (migration 010), deletion
  of an in-use value is refused (409), and migration 011 backfills any value an
  earlier rename left out of the list.
- **Forced password rotation was recorded but never enforced.**
  `must_change_password` was set when an administrator created an account or
  reset a password, but nothing read it, and there was no self-service way for
  a user to change their own password — so the flag could only ever be cleared
  by another administrator reset. The claim is now carried in the access token,
  the gateway refuses every proxied route (403) while it is set, the identity
  service exposes `POST /auth/change-password` (current password required,
  other refresh tokens revoked), and the SPA prompts for the new password at
  first sign-in.
- **A crafted `X-Forwarded-For` broke sign-in.** The application read the raw
  header itself, so any client could put arbitrary text there; the value went
  into an `inet` column and PostgreSQL rejected the whole request — a single
  header was enough to make `/auth/login` return 500. No service parses the
  header now: the address comes from `request.client`, which uvicorn rewrites
  only for peers in `PATCHTRACK_FORWARDED_IPS`, and only a parseable address
  is recorded (NULL otherwise). The backends trust the compose network in
  `PATCHTRACK_INTERNAL_FORWARDED_IPS`, so audit rows still show the operator.
- **Sign out did nothing.** The SPA posted to `/api/session/logout` with a bare
  `fetch`, which carries no `x-patchtrack-csrf` header, so the gateway's CSRF
  middleware answered 403 and the session cookie survived — while the browser
  navigated to the login page as if the sign-out had worked. Now routed through
  the API helper, which supplies the token.
- **The image would not build on its own base image.** Red Hat's UBI python
  image sets `USER 1001`, so every `RUN` before the Dockerfile's final
  `USER 1001` executed unprivileged; the `chmod`/`chown` step failed with
  "Operation not permitted". The build now elevates for that step only.
- **The compose default database image did not exist.**
  `registry.access.redhat.com/ubi9/postgresql-13:latest` returns `NAME_UNKNOWN`
  on an anonymous pull — Red Hat publishes the database images at
  `registry.redhat.io`, behind a subscription and a terms acceptance. The
  default is now `docker.io/library/postgres:13`, which pulls without an
  account; the Red Hat image remains usable through `POSTGRES_IMAGE` and
  `POSTGRES_DATA_DIR`, and the compose file sets both environment-variable
  spellings so either image starts.
- **The one-shot `migrate` service could never run.** Compose overrides `CMD`,
  not `ENTRYPOINT`, and `entrypoint.sh` ignored its arguments — so it demanded
  `PATCHTRACK_SERVICE`, exited 2, and every service that waits on `migrate`
  never started. The entrypoint now executes a supplied command.
- **`PATCHTRACK_LOG_LEVEL=INFO` crash-looped every container.** The documented
  value is capitalised, uvicorn accepts only lowercase level names, and the
  setting was passed straight to `--log-level`. The template now ships
  `info`, and the container entrypoint lowercases whatever it is given.
- **`email-validator` was missing from the image.** The identity service
  models email addresses with pydantic's `EmailStr`, which imports
  `email_validator` at runtime; the package was in the development virtualenv
  but not in `deploy/container/requirements.txt`, so the identity container
  exited on import. Added, and a test now resolves every import in `services/`
  to a declaration in that file.
- **The container applied no migrations at all.** The runner looked for
  `deploy/postgres/migrations` at a fixed depth above its own file — correct
  for `services/common/patchtrack_common` in the checkout, wrong in the image,
  where the package is one level shallower. It found zero files, `migrate`
  exited 0 and printed "nothing (up to date)", and every service then failed on
  its first query with `relation "identity.users" does not exist`. Discovery
  now walks up until it finds the directory, and an absent directory raises
  instead of masquerading as an up-to-date schema.
- **The job runner only knew Docker.** `patchtrack-job.sh` selected the
  container transport with `docker compose`, so on the RHEL/Rocky hosts this
  release targets — where Podman is the shipped runtime — it fell back to the
  native transport and could not reach a port the container deployment does
  not publish. It now detects `docker compose`, `podman-compose`, or `podman
  exec` on the labelled container, pinnable with
  `PATCHTRACK_CONTAINER_CLI=docker|podman`.
- **The gateway proxied to itself.** The four backend URLs default to
  `http://127.0.0.1:800x`, which inside the gateway container is its own
  loopback, so every proxied request failed to connect. Compose now sets them
  to the service names on the project network.
- **The server-list search box did nothing.** The console sent the term as
  `q`; `/compliance/servers` reads `search`. The request succeeded and ignored
  the filter, which is worse than an error: an operator searching for one host
  saw the full list and could believe the host was absent. The parameter was
  corrected and a browser check now asserts that searching narrows the rows and
  that clearing it restores them.
- **Searching a state name also returned the opposite state.** Compliance state
  was matched with `ILIKE '%term%'`, so `Compliant` matched `Non-compliant` as a
  substring and the inventory search for one status showed both. The five
  states are now matched as whole terms (`compliant`, `non_compliant`, …), and
  Server inventory gained an explicit **Status** filter that selects one state
  exactly through the existing `compliance_state` parameter.
- **The search box lost focus and caret on every pause.** Both paged registers
  re-render after a debounced search, which replaced the input mid-word: the
  text cursor disappeared and the operator had to click back in. The box now
  owns the latest text and caret, restores both on the render that replaces it,
  and a response from a superseded request is discarded rather than painted
  over newer typing. The debounce was also lengthened to 500 ms.
- **The business-service panel dropped every group after the seventh.** The
  row chart sliced the response to seven rows without any indication, so an
  eighth service — including a newly configured or locally corrected one —
  was absent from the dashboard. Every group is now drawn, and a long list
  scrolls inside the panel.
- **The operating-system panel grouped by version.** It showed `9.4`, `2022`
  and `11.4` instead of the platform, so RHEL, Windows and Solaris could not
  be compared. `/summary` now groups by `os_family`, and the panel labels the
  families RHEL, Microsoft Windows, Solaris and Other. The environment panel
  was checked at the same time and does move a server between groups when its
  environment override changes.

### Verification performed

- 226 unit tests and 43 database integration tests pass (269 with PostgreSQL).
- Browser smoke test: all 14 routes render without console errors; the
  dashboard renders its tiles and charts, the basis control switches between
  monthly, quarterly and yearly and swaps the trend panel, the auto-refresh
  control offers its four settings, and TV mode hides and restores the
  navigation via the button, `K`, `Esc` and the `?tv=1` deep link. All eleven
  registers were checked for a search box and a Sort by control; the audit
  search was exercised (49 → 35 rows), clearing it restored all rows, and the
  order control reversed the sort. Multi-column search was exercised with terms
  that occur only in a non-key column: "Core Banking" matched the business
  service on the server list, and "sccm" matched the source column in
  remediation (100 → 31 rows). On the server inventory the Status filter was
  asserted to select exactly one state, searching "Compliant" was asserted to
  return no Non-compliant row, and a search typed into the box was confirmed to
  keep focus, text and caret across the debounced re-render. A placement
  override was set through the dialog and observed to mark the row `LOCAL`,
  then cleared back to the AssetExplorer value; the integration suite also
  asserts that a subsequent `upsert_asset` updates the source columns while
  the override stands. In Settings an environment value was added, renamed and
  deleted through the browser, the placement dialog was confirmed to offer a
  select rather than free text, and **Delete** was confirmed disabled for a
  value still in use. The integration suite asserts that a rename propagates to
  every server holding the old spelling (source value preserved), survives the
  next sync, moves an override saved under the old name, and that deletion of
  an in-use value is refused; it also asserts that `/summary` returns the new
  names, which is what those servers are drawn under.
- The dashboard breakdowns were queried directly: `/api/compliance/summary`
  returns all nine business services on the sample estate (including the
  locally corrected `Others` group that previously fell off the panel), the
  OS panel returns the `rhel` / `windows` / `solaris` families rather than
  versions, and a temporary environment override was observed to move one
  server from `Staging` to the new group and back. Renaming `Payments` in
  Settings was observed through the gateway to relabel its six servers as
  `Payments Ops` in `/api/compliance/summary` while `business_group_source`
  still read `Payments`, then renamed back.
- Server-side sorting was queried directly: `sort=hostname&direction=asc`
  returns hostnames ascending, `sort=total_patches&direction=desc` returns the
  largest outstanding counts first, an unknown sort key is a 422, and a bad
  direction is rejected by the parameter schema.
- Auto-refresh exercised in a real browser at 30 seconds: two timed reloads
  were observed in 65 seconds, and none after navigating away from the
  dashboard — the timer is stopped on every route change. The trend endpoint
  was also queried directly for each basis, returning 7 monthly, 3 quarterly
  and 1 yearly points on the sample data with their snapshot counts.
- Gateway published on the host address (not loopback) and browsed over plain
  HTTP as a development instance: sign-in sets usable cookies under the
  explicit `PATCHTRACK_SESSION_COOKIE_SECURE=never` override, the smoke test
  passes on all 14 routes, and the default policy still marks cookies `Secure`
  for any non-loopback host — an unencrypted production deployment therefore
  fails closed rather than leaking the session. An IPv6 loopback literal was
  also recognised correctly.
- Dashboard: every panel query prepared against PostgreSQL in both the "All"
  and a filtered form (30 variants), executed through Grafana's own
  `/api/ds/query` (15 panels OK), and rendered for visual inspection.
- API, Grafana and PDF agreement checked on the same populated database.
- The five services verified healthy with Grafana stopped.
- `patchtrack_grafana` created with a generated password: `SELECT` succeeds on
  the dashboard tables, while `INSERT` and `DELETE` are refused and the
  `identity` schema is not readable.
- Both job runners exercised against a stub service: `patchtrack-tick.sh`
  records the `scheduler` principal on all six steps, stamps only on success
  and exits non-zero when one step fails; `patchtrack-job.sh` in native mode
  sends the same headers and propagates a failure as exit 1. The container
  transport was then exercised against the running Podman stack (the runtime
  is detected for each call: `docker compose` when available, otherwise
  `podman-compose` or `podman exec` on the labelled container), and
  `patchtrack-job.sh daily` returned a real daily snapshot from the
  compliance container.
- Forced rotation exercised through the real gateway against a real identity
  service and database: 403 on every proxied route, the dialog's validation
  and inline error path in Chromium, and both success redirects.
- The full compose stack brought up under Podman 5.8.2 with
  `podman-compose` 1.6.0 on SELinux-enforcing Rocky Linux 9: PostgreSQL 13
  healthy, `migrate` applied all eight migrations, all five services healthy,
  and through the public gateway port: `/healthz` 200, bad credentials 401,
  one-time bootstrap 200, a proxied route 403 while a password change is owed,
  the change 200, and the same route 200 afterwards with real backend data.
  `pg_stat_ssl` showed TLSv1.3 on every service connection. The browser smoke
  test then signed in through the same gateway and visited all 14 routes with
  no console error; on the empty estate the dashboard shows its documented
  empty states rather than a fabricated 0%. This run is what
  found the image, entrypoint, log-level, dependency, migration-discovery and
  gateway-URL defects listed above.

### Not verified in this environment, and why

| Item | Reason |
|------|--------|
| Live AssetExplorer, Satellite, SCCM endpoints | No access to the source systems; connectors are configuration-driven for this reason |
| Docker Engine / Docker Compose specifically | Docker is not installed on the build host. The same compose topology was run under Podman (see above); Docker is expected to behave the same, but that engine was not exercised |
| SELinux enforcing with **Docker's** label handling | Verified with Podman on an enforcing host; Docker's daemon-side `:Z` relabelling was not exercised |
| SMTP delivery | No relay available; the code path is reviewed, not exercised |
| PDF visual layout | Validity and digest are asserted; appearance needs a human eye |
| Performance at production scale | No production-sized data set |

### Known limitations

- No MFA and no OIDC/enterprise sign-in (the schema accommodates the latter;
  the login path refuses such accounts with an explanatory message).
- Role model is global: a Viewer sees the whole estate.
- Collection scheduling is external (the shipped systemd timer), not in-app.
  Cron scheduling is deliberately not supported.
- No Prometheus exporter; monitoring is query-based.
- The four systemd units in `deploy/systemd/` are written but have never been
  started: this host has no `patchtrack` account, no `/etc/patchtrack` and no
  system postgresql-13 service. They are validated with
  `systemd-analyze verify` only. See
  [operations-runbook.md](operations-runbook.md#5-running-natively-under-systemd).
- The Grafana read-only role was created and exercised on the build host —
  `SELECT` succeeds on the dashboard tables while `INSERT`, `DELETE` and any
  access to `identity` are refused — but the target still needs
  `deploy/postgres/roles/010_grafana_readonly.sql` run against its own
  database, because the password is generated per deployment and is not
  shipped.

### Upgrade notes

This is the first release; there is no upgrade path from a previous version.
For a fresh install see [install.md](install.md).
