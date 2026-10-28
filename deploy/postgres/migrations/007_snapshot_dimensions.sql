-- PatchTrack 007: snapshot group dimensions.
--
-- Snapshots store one row per breakdown group. The original constraint listed
-- dimensions that the evaluator does not emit while omitting the ones it
-- does (os_version, os_family), so any snapshot capture failed. Align the
-- constraint with compliance.engine.DIMENSIONS plus the severity roll-up.

ALTER TABLE compliance.snapshot_groups
    DROP CONSTRAINT IF EXISTS snapshot_groups_dimension_check;

ALTER TABLE compliance.snapshot_groups
    ADD CONSTRAINT snapshot_groups_dimension_check
    CHECK (dimension IN (
        'os_family', 'os_version', 'environment', 'business_group',
        'application_group', 'severity', 'source'
    ));
