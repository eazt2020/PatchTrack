-- PatchTrack 024: direct Solaris collection targets and Solaris 10 baselines.
--
-- Solaris no longer has to depend on one inbox directory maintained by an
-- external collection job. Each server (or non-global zone) is an explicit
-- target with its own credential reference and pinned host key, and PatchTrack
-- pulls it over SSH.
--
-- Solaris 11 reads the live SRU/package state from the host. Solaris 10 can
-- only enumerate installed patches, so a target must reference a baseline of
-- required patch revisions; with no baseline, no assessment is written and the
-- host stays Unknown rather than appearing compliant.

CREATE TABLE IF NOT EXISTS inventory.solaris_baselines (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name         text NOT NULL UNIQUE,
    os_major     integer NOT NULL CHECK (os_major IN (10, 11)),
    description  text NOT NULL DEFAULT '',
    -- [{patch_id, min_revision, title, severity, obsoletes: []}, ...]
    entries      jsonb NOT NULL DEFAULT '[]'::jsonb,
    source       text NOT NULL DEFAULT 'manual',
    created_by   text NOT NULL DEFAULT 'system',
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS inventory.solaris_targets (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name              text NOT NULL UNIQUE,
    hostname          text NOT NULL,
    port              integer NOT NULL DEFAULT 22 CHECK (port BETWEEN 1 AND 65535),
    username          text NOT NULL,
    secret_ref        text NOT NULL,
    -- SHA256:<base64> of the server host key. Empty means the target cannot
    -- be collected until an administrator pins the key.
    host_key          text NOT NULL DEFAULT '',
    os_major          integer NOT NULL CHECK (os_major IN (10, 11)),
    zone              text NOT NULL DEFAULT 'global',
    baseline_id       uuid REFERENCES inventory.solaris_baselines(id)
                          ON DELETE SET NULL,
    collector_command text NOT NULL DEFAULT '',
    enabled           boolean NOT NULL DEFAULT true,
    connect_timeout_s integer NOT NULL DEFAULT 15 CHECK (connect_timeout_s BETWEEN 1 AND 300),
    read_timeout_s    integer NOT NULL DEFAULT 120 CHECK (read_timeout_s BETWEEN 1 AND 900),
    last_success_at   timestamptz,
    last_error        text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS solaris_targets_enabled_idx
    ON inventory.solaris_targets (enabled);

CREATE INDEX IF NOT EXISTS solaris_targets_baseline_idx
    ON inventory.solaris_targets (baseline_id);
