-- PatchTrack 004: compliance policies, patch assessments, per-asset state and
-- historical snapshots. Snapshots are the immutable audit record.

CREATE TABLE IF NOT EXISTS compliance.policies (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL UNIQUE,
    description   text NOT NULL DEFAULT '',
    is_active     boolean NOT NULL DEFAULT false,
    active_version_id uuid,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL DEFAULT 'system'
);

-- Policy versions are immutable once created. Each evaluation and snapshot
-- references the version used, so historical results stay explainable.
CREATE TABLE IF NOT EXISTS compliance.policy_versions (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    policy_id            uuid NOT NULL REFERENCES compliance.policies(id) ON DELETE CASCADE,
    version              integer NOT NULL,
    definition           jsonb NOT NULL,
    effective_from       timestamptz NOT NULL DEFAULT now(),
    created_at           timestamptz NOT NULL DEFAULT now(),
    created_by           text NOT NULL DEFAULT 'system',
    change_note          text NOT NULL DEFAULT '',
    UNIQUE (policy_id, version)
);

ALTER TABLE compliance.policies
    DROP CONSTRAINT IF EXISTS policies_active_version_fk;
ALTER TABLE compliance.policies
    ADD CONSTRAINT policies_active_version_fk
    FOREIGN KEY (active_version_id) REFERENCES compliance.policy_versions(id);

-- Assessments are written by the inventory collector at the end of a run.
-- A failed collection writes NO assessment: absence of data never means
-- compliance.
CREATE TABLE IF NOT EXISTS compliance.assessments (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_id         uuid NOT NULL REFERENCES inventory.assets(id) ON DELETE CASCADE,
    source_system    text NOT NULL CHECK (source_system IN ('satellite', 'sccm', 'solaris')),
    assessed_at      timestamptz NOT NULL,
    collected_at     timestamptz NOT NULL DEFAULT now(),
    sync_run_id      uuid REFERENCES inventory.sync_runs(id),
    status           text NOT NULL CHECK (status IN ('complete', 'partial', 'failed')),
    total_patches    integer NOT NULL DEFAULT 0,
    critical_count   integer NOT NULL DEFAULT 0,
    important_count  integer NOT NULL DEFAULT 0,
    moderate_count   integer NOT NULL DEFAULT 0,
    low_count        integer NOT NULL DEFAULT 0,
    source_reference text NOT NULL DEFAULT '',
    details          jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS assessments_asset_idx
    ON compliance.assessments (asset_id, assessed_at DESC);
CREATE INDEX IF NOT EXISTS assessments_source_idx
    ON compliance.assessments (source_system, assessed_at DESC);

-- Current assessment pointer per asset (denormalised for fast dashboards).
CREATE TABLE IF NOT EXISTS compliance.asset_latest_assessment (
    asset_id        uuid PRIMARY KEY REFERENCES inventory.assets(id) ON DELETE CASCADE,
    assessment_id   uuid NOT NULL REFERENCES compliance.assessments(id) ON DELETE CASCADE,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Outstanding patch findings (errata / updates / Solaris patches).
CREATE TABLE IF NOT EXISTS compliance.findings (
    id               bigserial PRIMARY KEY,
    asset_id         uuid NOT NULL REFERENCES inventory.assets(id) ON DELETE CASCADE,
    assessment_id    uuid NOT NULL REFERENCES compliance.assessments(id) ON DELETE CASCADE,
    source_system    text NOT NULL,
    advisory_id      text NOT NULL,
    title            text NOT NULL DEFAULT '',
    severity         text NOT NULL CHECK (severity IN
                       ('critical', 'important', 'moderate', 'low', 'unknown')),
    category         text NOT NULL DEFAULT '',
    released_at      timestamptz,
    remediation_due  date,
    source_reference text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS findings_asset_idx ON compliance.findings (asset_id);
CREATE INDEX IF NOT EXISTS findings_assessment_idx
    ON compliance.findings (assessment_id);
CREATE INDEX IF NOT EXISTS findings_severity_idx
    ON compliance.findings (severity, remediation_due);
CREATE UNIQUE INDEX IF NOT EXISTS findings_unique_idx
    ON compliance.findings (assessment_id, advisory_id);

-- Evaluated compliance state per asset for the current policy version.
CREATE TABLE IF NOT EXISTS compliance.asset_compliance (
    asset_id           uuid PRIMARY KEY REFERENCES inventory.assets(id) ON DELETE CASCADE,
    state              text NOT NULL CHECK (state IN
                         ('compliant', 'non_compliant', 'unknown', 'stale', 'excluded')),
    reason             text NOT NULL DEFAULT '',
    policy_version_id  uuid REFERENCES compliance.policy_versions(id),
    evaluated_at       timestamptz NOT NULL DEFAULT now(),
    assessment_age_hours numeric(10, 2),
    critical_count     integer NOT NULL DEFAULT 0,
    total_patches      integer NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS asset_compliance_state_idx
    ON compliance.asset_compliance (state);

-- Snapshot headers: one per scope/period and capture. Never updated.
CREATE TABLE IF NOT EXISTS compliance.snapshots (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope              text NOT NULL CHECK (scope IN
                         ('daily', 'monthly', 'quarterly', 'on_demand')),
    period_start       date NOT NULL,
    period_end         date NOT NULL,
    captured_at        timestamptz NOT NULL DEFAULT now(),
    policy_version_id  uuid REFERENCES compliance.policy_versions(id),
    filters            jsonb NOT NULL DEFAULT '{}'::jsonb,
    total_servers      integer NOT NULL DEFAULT 0,
    compliant_servers  integer NOT NULL DEFAULT 0,
    non_compliant_servers integer NOT NULL DEFAULT 0,
    unknown_servers    integer NOT NULL DEFAULT 0,
    stale_servers      integer NOT NULL DEFAULT 0,
    excluded_servers   integer NOT NULL DEFAULT 0,
    compliance_pct     numeric(6, 2),
    target_pct         numeric(6, 2),
    critical_findings  integer NOT NULL DEFAULT 0,
    total_findings     integer NOT NULL DEFAULT 0,
    generated_by       text NOT NULL DEFAULT 'system',
    methodology        jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS snapshots_period_idx
    ON compliance.snapshots (scope, period_start DESC);
-- One daily snapshot per calendar day (UTC), enforced independently of the
-- exact capture timestamp so a retry cannot duplicate history.
CREATE UNIQUE INDEX IF NOT EXISTS snapshots_daily_unique_idx
    ON compliance.snapshots (period_start, ((captured_at AT TIME ZONE 'UTC')::date))
    WHERE scope = 'daily';

CREATE TABLE IF NOT EXISTS compliance.snapshot_groups (
    id            bigserial PRIMARY KEY,
    snapshot_id   uuid NOT NULL REFERENCES compliance.snapshots(id) ON DELETE CASCADE,
    dimension     text NOT NULL CHECK (dimension IN
                    ('os', 'environment', 'business_group', 'application_group',
                     'severity', 'source')),
    group_key     text NOT NULL,
    total_servers integer NOT NULL DEFAULT 0,
    compliant     integer NOT NULL DEFAULT 0,
    non_compliant integer NOT NULL DEFAULT 0,
    unknown       integer NOT NULL DEFAULT 0,
    stale         integer NOT NULL DEFAULT 0,
    finding_count integer NOT NULL DEFAULT 0,
    UNIQUE (snapshot_id, dimension, group_key)
);

-- Solaris compliance is collected through an approved read-only drop of
-- signed assessment files; ingestion is logged here.
CREATE TABLE IF NOT EXISTS compliance.solaris_uploads (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    received_at   timestamptz NOT NULL DEFAULT now(),
    filename      text NOT NULL,
    sha256        text NOT NULL,
    uploaded_by   text NOT NULL,
    status        text NOT NULL CHECK (status IN ('accepted', 'rejected')),
    records       integer NOT NULL DEFAULT 0,
    message       text NOT NULL DEFAULT ''
);
