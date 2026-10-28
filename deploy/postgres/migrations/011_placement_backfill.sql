-- PatchTrack 011: the list must contain every value a server currently holds.
--
-- Before 010, renaming a list entry did not propagate: it changed the picker
-- while servers kept the old spelling, and an administrator who then renamed
-- or deleted that entry could leave a value in use with no entry in the list.
-- Once 010 is in place that cannot happen again — deletion is refused while a
-- value is in use and a rename carries the servers with it — but an
-- installation that was already edited may have drifted.
--
-- This backfills the missing entries from the effective values. It invents
-- nothing: every row comes from an asset that exists. Values that 010 now
-- resolves to a renamed entry appear under the new name, so no duplicate
-- entry is created.

INSERT INTO inventory.placement_options (kind, value, created_by)
SELECT 'environment', v, 'migration'
FROM (
    SELECT DISTINCT btrim(environment) AS v
    FROM inventory.asset_effective
    WHERE environment IS NOT NULL AND btrim(environment) <> ''
) AS in_use
ON CONFLICT DO NOTHING;

INSERT INTO inventory.placement_options (kind, value, created_by)
SELECT 'business_group', v, 'migration'
FROM (
    SELECT DISTINCT btrim(business_group) AS v
    FROM inventory.asset_effective
    WHERE business_group IS NOT NULL AND btrim(business_group) <> ''
) AS in_use
ON CONFLICT DO NOTHING;
