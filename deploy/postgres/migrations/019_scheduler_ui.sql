-- Operator-editable scheduler configuration.
--
-- Intervals and the enabled flag are now edited from the Schedules screen.
-- The environment variable remains the default for a job that has never been
-- configured; once a row exists, the row is authoritative. `enabled` defaults
-- to true so existing rows keep running after the upgrade.

ALTER TABLE shared.scheduled_jobs
    ADD COLUMN IF NOT EXISTS enabled boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS updated_by text NOT NULL DEFAULT '';
