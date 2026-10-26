-- PatchTrack 010: rename propagation for placement values.
--
-- Renaming an environment or business service in Settings must be visible in
-- the inventory, the compliance engine, reports and the dashboard. Writing the
-- new value into inventory.assets would be wrong twice over: it would lose the
-- value AssetExplorer actually reported, and the next sync would put the old
-- value straight back.
--
-- A rename therefore records the old value as an alias of the list entry. The
-- effective view resolves the source value and any override through the alias,
-- so every reader sees the new name, the source value stays visible as
-- *_source, and a re-sync reporting the old value keeps resolving to the new
-- one. Nothing is written back to AssetExplorer.

CREATE TABLE IF NOT EXISTS inventory.placement_aliases (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       text NOT NULL
                 CHECK (kind IN ('environment', 'business_group')),
    alias      text NOT NULL
                 CHECK (btrim(alias) <> '' AND length(alias) <= 128),
    option_id  uuid NOT NULL
                 REFERENCES inventory.placement_options(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL
);

-- One alias may point at one entry only: two renames that converge on the same
-- old spelling would be ambiguous. Case-insensitive, as for the current values.
CREATE UNIQUE INDEX IF NOT EXISTS placement_aliases_kind_alias_key
    ON inventory.placement_aliases (kind, lower(btrim(alias)));

CREATE INDEX IF NOT EXISTS placement_aliases_option_idx
    ON inventory.placement_aliases (option_id);

COMMENT ON TABLE inventory.placement_aliases IS
    'Values that a placement option was previously called. Resolved on read by '
    'inventory.asset_effective; the source value in inventory.assets is never '
    'overwritten and the next sync cannot undo the rename.';

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
    COALESCE(oeo.value, o.environment, seo.value, a.environment)
        AS environment,
    COALESCE(oao.value, o.business_group, sao.value, a.business_group)
        AS business_group,
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
LEFT JOIN inventory.asset_overrides o ON o.asset_id = a.id
-- An override is a local decision and wins over the source value; resolve its
-- stored spelling through the alias table too, so a rename made after the
-- override was saved is reflected without rewriting the override row.
LEFT JOIN inventory.placement_aliases oea
       ON oea.kind = 'environment'
      AND lower(btrim(o.environment)) = lower(btrim(oea.alias))
LEFT JOIN inventory.placement_options oeo ON oeo.id = oea.option_id
LEFT JOIN inventory.placement_aliases oaa
       ON oaa.kind = 'business_group'
      AND lower(btrim(o.business_group)) = lower(btrim(oaa.alias))
LEFT JOIN inventory.placement_options oao ON oao.id = oaa.option_id
-- Otherwise the value AssetExplorer reports is resolved the same way.
LEFT JOIN inventory.placement_aliases sea
       ON sea.kind = 'environment'
      AND lower(btrim(a.environment)) = lower(btrim(sea.alias))
LEFT JOIN inventory.placement_options seo ON seo.id = sea.option_id
LEFT JOIN inventory.placement_aliases sba
       ON sba.kind = 'business_group'
      AND lower(btrim(a.business_group)) = lower(btrim(sba.alias))
LEFT JOIN inventory.placement_options sao ON sao.id = sba.option_id;

COMMENT ON VIEW inventory.asset_effective IS
    'Assets with the effective environment and business group (override, then '
    'renamed list value, then source), their source values and any local '
    'override metadata. Read-only.';
