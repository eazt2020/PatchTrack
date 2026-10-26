-- PatchTrack 008: local placement overrides.
--
-- AssetExplorer stays the authority for the attributes it owns. A sync writes
-- inventory.assets as before and never touches this table, so an override
-- survives every collection without pausing the integration. The override is
-- an explicit, attributed local decision layered on top of the source value;
-- clearing it returns the asset to what AssetExplorer says at the next read,
-- with no re-sync required.

CREATE TABLE IF NOT EXISTS inventory.asset_overrides (
    asset_id       uuid PRIMARY KEY
                     REFERENCES inventory.assets(id) ON DELETE CASCADE,
    environment    text
                     CHECK (environment IS NULL
                            OR (btrim(environment) <> '' AND length(environment) <= 128)),
    business_group text
                     CHECK (business_group IS NULL
                            OR (btrim(business_group) <> ''
                                AND length(business_group) <= 128)),
    reason         text NOT NULL
                     CHECK (length(btrim(reason)) >= 3 AND length(reason) <= 500),
    set_by         text NOT NULL,
    set_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (environment IS NOT NULL OR business_group IS NOT NULL)
);

COMMENT ON TABLE inventory.asset_overrides IS
    'Operator overrides for environment and business group. Sync never writes '
    'here; the source value is retained in inventory.assets.';

-- Every reader that needs the placement (inventory lists, compliance engine,
-- reports, Grafana) reads this view, so the effective value is the same
-- everywhere. environment_source / business_group_source keep the source
-- answer visible next to the override.
CREATE OR REPLACE VIEW inventory.asset_effective AS
SELECT
    a.id,
    a.assetexplorer_id,
    a.hostname,
    a.fqdn,
    a.ip_address,
    a.os_family,
    a.os_name,
    a.os_version,
    a.asset_type,
    a.asset_status,
    COALESCE(o.environment, a.environment) AS environment,
    COALESCE(o.business_group, a.business_group) AS business_group,
    a.application_group,
    a.asset_owner,
    a.department,
    a.location,
    a.lifecycle_status,
    a.classification,
    a.patch_source,
    a.in_scope,
    a.exclusion_reason,
    a.reconciliation_state,
    a.source_created_at,
    a.source_modified_at,
    a.first_seen_at,
    a.last_synced_at,
    a.last_sync_run_id,
    a.raw,
    a.environment AS environment_source,
    a.business_group AS business_group_source,
    (o.environment IS NOT NULL) AS environment_overridden,
    (o.business_group IS NOT NULL) AS business_group_overridden,
    o.reason AS override_reason,
    o.set_by AS override_set_by,
    o.set_at AS override_set_at
FROM inventory.assets a
LEFT JOIN inventory.asset_overrides o ON o.asset_id = a.id;

COMMENT ON VIEW inventory.asset_effective IS
    'Assets with the effective environment and business group, their source '
    'values and any local override metadata. Read-only.';
