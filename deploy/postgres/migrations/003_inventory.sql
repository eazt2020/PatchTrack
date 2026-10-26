-- PatchTrack 003: authoritative asset inventory synchronised from
-- ManagedEngine AssetExplorer, plus integration and collection bookkeeping.

CREATE TABLE IF NOT EXISTS inventory.integrations (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    key                text NOT NULL UNIQUE
                         CHECK (key IN ('assetexplorer', 'satellite', 'sccm', 'solaris')),
    display_name       text NOT NULL,
    kind               text NOT NULL,
    endpoint           text NOT NULL DEFAULT '',
    auth_method        text NOT NULL DEFAULT 'none',
    secret_ref         text NOT NULL DEFAULT '',
    enabled            boolean NOT NULL DEFAULT false,
    sync_interval_minutes integer NOT NULL DEFAULT 360
                         CHECK (sync_interval_minutes BETWEEN 5 AND 10080),
    connect_timeout_s  integer NOT NULL DEFAULT 30,
    read_timeout_s     integer NOT NULL DEFAULT 120,
    page_size          integer NOT NULL DEFAULT 200,
    verify_tls         boolean NOT NULL DEFAULT true,
    extra              jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    updated_by         text NOT NULL DEFAULT 'system'
);

-- One row per collection attempt. Failures never delete inventory.
CREATE TABLE IF NOT EXISTS inventory.sync_runs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    integration_key text NOT NULL REFERENCES inventory.integrations(key),
    trigger         text NOT NULL CHECK (trigger IN ('schedule', 'manual', 'startup')),
    status          text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'success', 'partial', 'failed')),
    started_at      timestamptz NOT NULL DEFAULT now(),
    finished_at     timestamptz,
    records_seen    integer NOT NULL DEFAULT 0,
    records_created integer NOT NULL DEFAULT 0,
    records_updated integer NOT NULL DEFAULT 0,
    records_rejected integer NOT NULL DEFAULT 0,
    assets_unmatched integer NOT NULL DEFAULT 0,
    error_summary   text NOT NULL DEFAULT '',
    details         jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_by    text NOT NULL DEFAULT 'schedule'
);

CREATE INDEX IF NOT EXISTS sync_runs_integration_idx
    ON inventory.sync_runs (integration_key, started_at DESC);

-- AssetExplorer remains the authority. This table is a normalised,
-- synchronised local representation; assetexplorer_id is immutable.
CREATE TABLE IF NOT EXISTS inventory.assets (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    assetexplorer_id      text NOT NULL UNIQUE,
    hostname              text NOT NULL,
    fqdn                  text,
    ip_address            inet,
    os_family             text NOT NULL
                            CHECK (os_family IN ('rhel', 'windows', 'solaris', 'other')),
    os_name               text NOT NULL DEFAULT '',
    os_version            text NOT NULL DEFAULT '',
    asset_type            text NOT NULL DEFAULT 'server',
    asset_status          text NOT NULL DEFAULT '',
    environment           text NOT NULL DEFAULT 'UNKNOWN',
    business_group        text NOT NULL DEFAULT 'UNKNOWN',
    application_group     text NOT NULL DEFAULT 'UNKNOWN',
    asset_owner           text NOT NULL DEFAULT '',
    department            text NOT NULL DEFAULT '',
    location              text NOT NULL DEFAULT '',
    lifecycle_status      text NOT NULL DEFAULT 'active'
                            CHECK (lifecycle_status IN
                              ('active', 'retired', 'decommissioned', 'unknown')),
    classification        text NOT NULL DEFAULT '',
    patch_source          text NOT NULL DEFAULT ''
                            CHECK (patch_source IN ('', 'satellite', 'sccm', 'solaris')),
    in_scope              boolean NOT NULL DEFAULT true,
    exclusion_reason      text NOT NULL DEFAULT '',
    reconciliation_state  text NOT NULL DEFAULT 'matched'
                            CHECK (reconciliation_state IN
                              ('matched', 'suggested', 'unmatched', 'conflict')),
    source_created_at     timestamptz,
    source_modified_at    timestamptz,
    first_seen_at         timestamptz NOT NULL DEFAULT now(),
    last_synced_at        timestamptz NOT NULL DEFAULT now(),
    last_sync_run_id      uuid REFERENCES inventory.sync_runs(id),
    raw                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    CHECK (hostname <> '')
);

CREATE INDEX IF NOT EXISTS assets_hostname_idx ON inventory.assets (hostname);
CREATE INDEX IF NOT EXISTS assets_fqdn_idx ON inventory.assets (fqdn);
CREATE INDEX IF NOT EXISTS assets_ip_idx ON inventory.assets (ip_address);
CREATE INDEX IF NOT EXISTS assets_scope_idx
    ON inventory.assets (in_scope, lifecycle_status, os_family);
CREATE INDEX IF NOT EXISTS assets_groups_idx
    ON inventory.assets (business_group, application_group, environment);

-- Historical identifiers. A hostname/IP moving between assets is recorded
-- here so that a rename never silently creates a second inventory record.
CREATE TABLE IF NOT EXISTS inventory.asset_aliases (
    id          bigserial PRIMARY KEY,
    asset_id    uuid NOT NULL REFERENCES inventory.assets(id) ON DELETE CASCADE,
    alias_type  text NOT NULL CHECK (alias_type IN ('hostname', 'fqdn', 'ip')),
    alias_value text NOT NULL,
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (alias_type, alias_value)
);

-- Unresolved duplicate/identifier conflicts surfaced to administrators.
CREATE TABLE IF NOT EXISTS inventory.reconciliation_conflicts (
    id            bigserial PRIMARY KEY,
    detected_at   timestamptz NOT NULL DEFAULT now(),
    asset_id      uuid REFERENCES inventory.assets(id) ON DELETE SET NULL,
    source_system text NOT NULL,
    source_ref    text NOT NULL DEFAULT '',
    match_key     text NOT NULL DEFAULT '',
    reason        text NOT NULL,
    payload       jsonb NOT NULL DEFAULT '{}'::jsonb,
    resolved_at   timestamptz,
    resolved_by   text,
    resolution    text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS reconciliation_conflicts_open_idx
    ON inventory.reconciliation_conflicts (detected_at DESC)
    WHERE resolved_at IS NULL;

-- Link table between a patch-management source record and an asset.
CREATE TABLE IF NOT EXISTS inventory.asset_source_links (
    id            bigserial PRIMARY KEY,
    asset_id      uuid NOT NULL REFERENCES inventory.assets(id) ON DELETE CASCADE,
    source_system text NOT NULL CHECK (source_system IN ('satellite', 'sccm', 'solaris')),
    source_ref    text NOT NULL,
    confidence    numeric(4, 3) NOT NULL DEFAULT 1.000,
    method        text NOT NULL DEFAULT 'direct',
    linked_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (source_system, source_ref)
);

CREATE INDEX IF NOT EXISTS asset_source_links_asset_idx
    ON inventory.asset_source_links (asset_id);
