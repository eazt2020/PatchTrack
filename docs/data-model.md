# Data model

One PostgreSQL database (`patchtrack`), five schemas, one writer per schema.
Migrations are plain SQL in `deploy/postgres/migrations/`, applied in filename
order by a runner that records each file's SHA-256.

```
shared        migrations ledger, audit trail, application settings
identity      users, refresh tokens, login attempts
inventory     integrations, sync runs, assets, asset overrides, placement options,
              placement aliases, asset aliases, source links, conflicts
compliance    policies, policy versions, assessments, findings, states,
              snapshots, snapshot groups, Solaris uploads
reporting     reports, schedules, job executions, delivery attempts
```

## Ownership rule

| Schema | Writes | Reads |
|--------|--------|-------|
| `shared` | all services (audit), inventory (settings) | all |
| `identity` | identity | identity |
| `inventory` | inventory | inventory, compliance, reporting |
| `compliance` | compliance | compliance, reporting |
| `reporting` | reporting | reporting |

Cross-schema reads are deliberate (see
[architecture.md](architecture.md#why-the-schema-read-crossing-is-allowed)).
Cross-schema writes do not exist.

## shared

### `shared.schema_migrations`

| Column | Type | Notes |
|--------|------|-------|
| version | text PK | filename stem, e.g. `004_compliance` |
| description | text | |
| applied_at | timestamptz | |
| checksum | text | SHA-256 of the file; a mismatch aborts the run |

### `shared.audit_events` — append-only

| Column | Type | Notes |
|--------|------|-------|
| id | bigserial PK | |
| occurred_at | timestamptz | |
| actor | text | username, or `system` |
| actor_role | text | role at the time of the event |
| event_type | text | e.g. `auth.login`, `report.generated` |
| target_type / target_id | text | resource affected |
| outcome | text | `success` \| `failure` \| `denied` |
| context | jsonb | scrubbed: password/token/secret keys replaced with `[redacted]` |
| source_ip | inet | |
| service | text | emitting service |

A trigger rejects `UPDATE` and any ordinary `DELETE` on this table, so a
compromised application role cannot rewrite history. Indexed on time, actor and
event type.

Retention is the one sanctioned deletion. `shared.purge_audit_events()` is
`SECURITY DEFINER`, reads the configured window from `shared.app_settings`,
sets a transaction-local flag that lets the trigger pass its own `DELETE`, and
takes no cutoff argument — a caller cannot delete more than the policy allows.
It enforces the 30–36,500 day range itself, and the trigger's `UPDATE` refusal
is never relaxed. Each run is recorded as `audit.purged` after the delete, so
the record of the purge cannot be removed by the same run.

### `shared.app_settings`

Key/value jsonb. Holds the organisation name used on reports and
`audit_retention_days` (`{"days": N}`, default 2555 when absent), plus
whatever runtime configuration must survive restarts. Secrets are **not**
stored here, or anywhere else in the database.

## identity

### `identity.users`

| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| username | citext UNIQUE | case-insensitive |
| display_name, email | text / citext | |
| password_hash | text | Argon2id; nullable for external identity |
| role | text | CHECK in the four roles |
| auth_source | text | `local` \| `oidc` |
| is_active | boolean | |
| must_change_password | boolean | set on create and on admin reset |
| failed_attempts, locked_until | integer, timestamptz | lockout state |
| last_login_at, created_at, updated_at, created_by | | |

No rows are inserted by any migration. The first administrator is created
through the one-time bootstrap form, which is refused once a user exists.

### `identity.refresh_tokens`

`token_hash` (SHA-256 of the opaque token, UNIQUE), `expires_at`,
`revoked_at`, `user_agent`, `source_ip`. The token itself is never stored, so a
database dump does not yield usable sessions.

### `identity.login_attempts`

Append-only record of every attempt with the submitted username — kept
separate from `users` so that lockout behaviour cannot be used to enumerate
accounts.

## inventory

### `inventory.integrations`

Seeded by migration 006 as four rows, **disabled**, with empty endpoint and
empty `secret_ref`.

| Column | Notes |
|--------|-------|
| key | PK, CHECK in `assetexplorer`, `satellite`, `sccm`, `solaris` |
| display_name, kind, endpoint, auth_method | |
| secret_ref | a reference (`env:NAME`, `file:/path`) or empty — never a credential |
| enabled | false until an administrator configures it |
| sync_interval_minutes | 5..10080 |
| connect_timeout_s, read_timeout_s, page_size | |
| verify_tls | default true; disabling is audited |
| extra | jsonb: field map, Satellite username, Solaris inbox path |

### `inventory.sync_runs`

One row per collection attempt: integration, trigger (`schedule`, `manual`,
`startup`), status (`running`, `success`, `partial`, `failed`), timestamps,
counts (seen/created/updated/rejected/unmatched) and an error summary.

A failed run leaves every asset and assessment row untouched.

### `inventory.assets`

The normalised mirror of AssetExplorer.

| Column | Notes |
|--------|-------|
| id | uuid PK — the internal identity used everywhere else |
| assetexplorer_id | text UNIQUE — immutable source identity |
| hostname, fqdn, ip_address | current values |
| os_family | `rhel` \| `windows` \| `solaris` \| `other`, derived from os_name/version |
| os_name, os_version | as reported by the source |
| environment, business_group, application_group | reporting dimensions; `environment` and `business_group` hold the **source** values (see `asset_overrides`) |
| asset_owner, department, location, classification | context |
| lifecycle_status | `active` \| `retired` \| `decommissioned` \| `unknown` |
| patch_source | which system is authoritative for this asset |
| in_scope, exclusion_reason | manual scope decision |
| reconciliation_state | `matched` \| `suggested` \| `unmatched` \| `conflict` |
| source_created_at, source_modified_at | from the source |
| first_seen_at, last_synced_at, last_sync_run_id | provenance |
| raw | the source record as received, for diagnosis |

Uniqueness is on `assetexplorer_id` only. Hostnames are **not** unique: a
hostname can legitimately move between assets, and the alias table records that
it did.

### `inventory.asset_overrides`

At most one row per asset. The operator's local correction to the two placement
dimensions AssetExplorer is most often late to update.

| Column | Notes |
|--------|-------|
| asset_id | uuid PK, FK to `inventory.assets` (`ON DELETE CASCADE`) |
| environment | corrected environment, or NULL for "use the source value" |
| business_group | corrected business group, or NULL |
| reason | mandatory, 3–500 characters |
| set_by, set_at | attribution |

The sync never writes this table. `inventory.assets` keeps receiving the source
values on every collection, so an override survives a sync and the source answer
stays available. Clearing the override (or setting both fields to NULL) deletes
the row and the source value applies immediately. At least one field must be
set; blank values are rejected by a check constraint. Changes are recorded in
`shared.audit_events`.

### `inventory.placement_options`

The administrator-managed vocabulary offered by the placement editor and the
names displayed throughout the application. It is not a foreign key: no stored
asset or override value is rewritten when an entry changes.

| Column | Notes |
|--------|-------|
| id | uuid PK |
| kind | `environment` \| `business_group` |
| value | trimmed, 1–128 characters |
| created_by, created_at, updated_by, updated_at | attribution |

UNIQUE on `(kind, lower(btrim(value)))`, so a value cannot be listed twice in
different case or with stray whitespace. Migration 009 seeds the table from the
distinct values already present in `inventory.assets`, and migration 011
backfills any value still in use that an earlier rename left out of the list;
neither invents anything. The AssetExplorer sync neither reads nor writes this
table. The usage count shown in Settings is computed from
`inventory.asset_effective`, so it reflects source and override values alike.
A value with a usage count above zero cannot be deleted: the inventory must
never display a placement the list does not recognise. Changes are audited as
`settings.placement_option_added`, `_renamed` and `_removed`.

### `inventory.placement_aliases`

Previous names of a placement option, written when an administrator renames an
entry (migration 010). Resolution happens at read time, so the source record and
any override keep the value they were written with.

| Column | Notes |
|--------|-------|
| id | uuid PK |
| kind | `environment` \| `business_group` |
| alias | the previous spelling, trimmed, 1–128 characters |
| option_id | FK to `inventory.placement_options` (`ON DELETE CASCADE`) |
| created_by, created_at | attribution |

UNIQUE on `(kind, lower(btrim(alias)))`: a former name resolves to exactly one
entry. Renaming an entry onto another entry's alias, or adding a new entry with
a name that is still an alias, is refused. Deleting an option removes its
aliases with it.

### `inventory.asset_effective` (view)

Every reader that needs placement — the inventory list and filters, the
compliance engine, reporting and Grafana — reads this view, so one definition of
"effective environment" is used everywhere.

It exposes all `inventory.assets` columns with `environment` and
`business_group` replaced by the effective value: a local override, else the
source value resolved through `inventory.placement_aliases` to its current
Settings name, else the source value unchanged. The source-selected alias is
also applied to the override, so an override saved under an old spelling shows
the renamed value. It also exposes:

| Column | Meaning |
|--------|---------|
| environment_source, business_group_source | what AssetExplorer reports |
| environment_overridden, business_group_overridden | whether a local override applies |
| override_reason, override_set_by, override_set_at | attribution, NULL when none |

The view is read-only. `inventory.assets` remains the writable source mirror.

### `inventory.asset_aliases`

`(alias_type, alias_value)` UNIQUE, with `first_seen`/`last_seen`. Every
hostname, FQDN and IP an asset has ever had. This is what makes a rename update
one asset instead of creating a second one.

### `inventory.asset_source_links`

`(source_system, source_ref)` UNIQUE → `asset_id`. A direct link established
once (e.g. Satellite host id 412) stays attached to that asset even if the
hostname changes.

### `inventory.reconciliation_conflicts`

Open conflicts: `reason` (`ambiguous_match`, `identifier_changed`,
`unmatched_source`, …), the payload that caused it, and resolution fields.
Partial index on unresolved rows.

## compliance

### `compliance.policies` and `compliance.policy_versions`

A policy has a name and points at one active version. Versions are immutable
rows containing the full JSON definition (see
[compliance-methodology.md](compliance-methodology.md#5-policy)), a version
number, an effective date and a change note. Every evaluation and snapshot
records the version id, so a historical figure is always explainable.

### `compliance.assessments`

One row per collection of one host from one source.

| Column | Notes |
|--------|-------|
| asset_id | FK to `inventory.assets` |
| source_system | `satellite` \| `sccm` \| `solaris` |
| assessed_at | when the **source** says the assessment was taken |
| collected_at | when PatchTrack collected it |
| sync_run_id | provenance |
| status | `complete` \| `partial` \| `failed` |
| total_patches, critical_count, important_count, moderate_count, low_count | |
| details | raw source payload summary |

`assessed_at`, not `collected_at`, drives freshness. A source that keeps
returning an old timestamp will show as Stale even if collection is current —
which is the correct answer.

### `compliance.asset_latest_assessment`

Pointer to the newest assessment per asset, maintained in the same transaction
that inserts the assessment. Denormalised so the evaluator needs one join
instead of a `DISTINCT ON`.

### `compliance.findings`

Outstanding advisory per asset: `advisory_id`, `severity`
(`critical`/`important`/`moderate`/`low`/`unknown`), `title`, `category`,
`released_at`, `remediation_due`, `source_reference`. Unique on
`(assessment_id, advisory_id)` so a re-ingested assessment cannot duplicate
findings.

A severity the connector could not map is stored as `unknown` rather than being
dropped — an unmapped advisory is still an outstanding advisory.

### `compliance.asset_compliance`

Current evaluated state per asset: `state`, `reason` (human-readable, e.g.
"Assessment is 51.2h old; freshness threshold is 48h"), `policy_version_id`,
`evaluated_at`, `assessment_age_hours` (display value, rounded), and
denormalised counts for the drill-down.

### `compliance.snapshots` — immutable

Header of a frozen evaluation: scope (`daily`/`monthly`/`quarterly`/`on_demand`),
period start and end, `captured_at`, `policy_version_id`, `filters`, the five
counts, `compliance_pct`, `target_pct`, finding counts, `generated_by`, and the
`methodology` jsonb that spells out numerator, denominator, unknown treatment,
freshness threshold and blocking severities.

Partial unique index:
`(period_start, (captured_at AT TIME ZONE 'UTC')::date) WHERE scope='daily'`.

There is no update path. A re-evaluation creates a new row; it never edits one.

### `compliance.snapshot_groups`

One row per breakdown group per snapshot: dimension (`os_family`, `os_version`,
`environment`, `business_group`, `application_group`, `severity`), group key,
and the counts. Unique on `(snapshot_id, dimension, group_key)`.

This table is why a report for a past period can be regenerated with the same
breakdowns even after the estate has changed — the groups are stored, not
recomputed from current inventory. It is also what the dashboard's month,
quarter and year view reads: the latest daily snapshot in the period supplies
the tiles and panels, and a snapshot captured before `os_family` was recorded
simply has no rows for that dimension. Readers show that panel as not recorded
rather than filling it from the live inventory.

### `compliance.solaris_uploads`

One row per ingest: filename, SHA-256 of the submitted payload, uploader,
accepted/rejected, record count, message.

## reporting

### `reporting.reports`

| Column | Notes |
|--------|-------|
| report_type | `monthly_management` \| `quarterly_compliance` \| `inventory_register` |
| period_start, period_end | |
| snapshot_id | the snapshot the report renders |
| status | `queued` \| `generating` \| `completed` \| `failed` |
| requested_by, requested_at, started_at, completed_at | |
| filename, file_path | |
| sha256 | digest of the archived bytes |
| file_size, page_count | |
| error_summary | populated on failure |
| schedule_id | set when produced by a schedule |
| expires_at | retention deadline for the file |

Partial unique index `(schedule_id, report_type, period_start) WHERE
schedule_id IS NOT NULL AND status <> 'failed'` prevents a duplicated
scheduled period while still allowing a retry after failure.

### `reporting.schedules`

Name, report type, cadence (`monthly`/`quarterly`), `day_of_month` (1–28),
`run_hour`, `timezone` (IANA name), `recipients` (text[]), filters, enabled,
`next_run_at`, last run status.

`day_of_month` is constrained to 1–28 so a stored schedule always has a valid
day; the scheduler additionally clamps to the last day of the target month when
computing a run time (relevant when reading legacy rows or when the constraint
is relaxed).

### `reporting.job_executions`

`UNIQUE (schedule_id, period_start)`. Inserting the claim row is how a
scheduler instance takes ownership of a period; a second instance gets a unique
violation and skips. This is the idempotency ledger.

### `reporting.delivery_attempts`

Per attempt: recipients, `status` (`sent`/`failed`/`skipped`), detail. Separate
from `reports.status` by design — a delivery failure never changes a completed
report to failed.

## Entity relationships

```
identity.users 1──n identity.refresh_tokens

inventory.integrations 1──n inventory.sync_runs
inventory.sync_runs    1──n inventory.assets          (last_sync_run_id)
inventory.assets       1──1 inventory.asset_overrides
inventory.assets       1──n inventory.asset_aliases
inventory.assets       1──n inventory.asset_source_links
inventory.assets       1──n inventory.reconciliation_conflicts

inventory.assets       1──n compliance.assessments
compliance.assessments 1──n compliance.findings
compliance.assessments 1──1 compliance.asset_latest_assessment  (per asset)
inventory.assets       1──1 compliance.asset_compliance

compliance.policies        1──n compliance.policy_versions
compliance.policy_versions 1──n compliance.snapshots
compliance.snapshots       1──n compliance.snapshot_groups

compliance.snapshots 1──n reporting.reports
reporting.schedules  1──n reporting.reports
reporting.schedules  1──n reporting.job_executions
reporting.reports    1──n reporting.delivery_attempts
```

Deleting an asset cascades to its assessments, findings, alias and link rows —
but assets are not deleted in normal operation, so this matters only for a
controlled purge.

## Retention

| Data | Retention |
|------|-----------|
| Assets, assessments, findings | Indefinite (the audit record) |
| Snapshots | Indefinite |
| Audit events | Indefinite (append-only) |
| Report PDF files | `PATCHTRACK_REPORT_RETENTION_DAYS` (default 2555 days) |
| Report metadata | Indefinite, after the file is purged |
| Login attempts | Indefinite; pruned manually if required |

The purge job removes files and sets nothing else: the row keeps its filename,
SHA-256 and size, so an archived period remains identifiable after the bytes
have aged out.
